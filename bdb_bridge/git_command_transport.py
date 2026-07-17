from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import BridgeErrorCode
from .protocol import (
    BridgeError,
    COMMAND_PATH_RE,
    MANIFEST_PATH_RE,
    validate_repo_relative_path,
)
from .transport import CommandSnapshot, RemoteDocument


def is_hex(s: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in s)


def validate_sha40(name: str, val: str) -> None:
    if len(val) != 40 or not is_hex(val):
        raise BridgeError(
            BridgeErrorCode.TRANSPORT_UNAVAILABLE,
            f"Invalid {name}: must be a 40-character hex string, got {val!r}",
        )


def _canonical_git_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeError(
            BridgeErrorCode.TRANSPORT_UNAVAILABLE,
            f"Invalid git commit timestamp: {value!r}",
        ) from exc
    if parsed.tzinfo is None:
        raise BridgeError(
            BridgeErrorCode.TRANSPORT_UNAVAILABLE,
            f"Git commit timestamp has no timezone: {value!r}",
        )
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run_bytes(
        self,
        args: Iterable[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["git", "-C", str(cwd or self.repo), *list(args)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=env,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git {' '.join(args)} timed out: {exc}",
            ) from exc
        except FileNotFoundError as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git executable not found: {exc}",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git execution failed: {exc}",
            ) from exc

        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git {' '.join(args)} failed with exit code {completed.returncode}: {detail}",
            )
        return completed


class GitCommandTransport:
    def __init__(
        self,
        repo_path: Path,
        *,
        remote: str = "origin",
        commands_branch: str = "commands",
    ) -> None:
        self._git = Git(repo_path)
        self._remote = remote
        self._commands_branch = commands_branch
        self._cached_snapshot: CommandSnapshot | None = None
        self._cached_documents: dict[str, RemoteDocument] = {}

    def fetch_snapshot(self) -> CommandSnapshot:
        try:
            self._git.run_bytes(
                [
                    "fetch",
                    "--prune",
                    self._remote,
                    f"+refs/heads/{self._commands_branch}:refs/remotes/{self._remote}/{self._commands_branch}",
                ],
                check=True,
            )

            ref = f"refs/remotes/{self._remote}/{self._commands_branch}"
            completed = self._git.run_bytes(["rev-parse", ref], check=True)
            snapshot_sha = completed.stdout.decode("utf-8", errors="strict").strip()
            validate_sha40("snapshot SHA", snapshot_sha)

            if self._cached_snapshot is not None and self._cached_snapshot.snapshot_sha == snapshot_sha:
                return self._cached_snapshot

            snapshot: CommandSnapshot
            if self._cached_snapshot is not None and self._is_fast_forward(
                self._cached_snapshot.snapshot_sha,
                snapshot_sha,
            ):
                try:
                    snapshot = self._incremental_snapshot(
                        self._cached_snapshot.snapshot_sha,
                        snapshot_sha,
                    )
                except BridgeError:
                    snapshot = self._full_snapshot(snapshot_sha)
            else:
                snapshot = self._full_snapshot(snapshot_sha)

            self._cached_snapshot = snapshot
            self._cached_documents = {
                document.path: document
                for document in snapshot.manifests + snapshot.commands
            }
            return snapshot
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Git snapshot fetch failed: {exc}",
            ) from exc

    def _is_fast_forward(self, previous_sha: str, snapshot_sha: str) -> bool:
        completed = self._git.run_bytes(
            ["merge-base", "--is-ancestor", previous_sha, snapshot_sha],
            check=False,
        )
        return completed.returncode == 0

    def _incremental_snapshot(self, previous_sha: str, snapshot_sha: str) -> CommandSnapshot:
        completed = self._git.run_bytes(
            [
                "diff",
                "--name-status",
                "-z",
                "--no-renames",
                previous_sha,
                snapshot_sha,
                "--",
                "sessions",
            ],
            check=True,
        )
        parts = completed.stdout.split(b"\x00")
        if parts and parts[-1] == b"":
            parts.pop()
        if len(parts) % 2 != 0:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                "Incremental command diff has an invalid shape",
            )

        documents = dict(self._cached_documents)
        for index in range(0, len(parts), 2):
            status = parts[index].decode("ascii", errors="strict")
            path = parts[index + 1].decode("utf-8", errors="strict")
            kind = self._document_kind(path)
            if kind is None:
                continue
            if status == "D":
                documents.pop(path, None)
                continue
            if status not in {"A", "M", "T"}:
                raise BridgeError(
                    BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                    f"Unsupported incremental command diff status: {status}",
                )
            documents[path] = self._read_document(snapshot_sha, path)

        return self._snapshot_from_documents(snapshot_sha, documents)

    def _full_snapshot(self, snapshot_sha: str) -> CommandSnapshot:
        completed_tree = self._git.run_bytes(
            ["ls-tree", "-rz", "--name-only", snapshot_sha, "--", "sessions"],
            check=False,
        )
        if completed_tree.returncode not in (0, 128):
            detail = completed_tree.stderr.decode("utf-8", errors="replace").strip()
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"ls-tree failed: {detail}",
            )

        documents: dict[str, RemoteDocument] = {}
        for path_bytes in completed_tree.stdout.split(b"\x00"):
            if not path_bytes:
                continue
            try:
                path = path_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue
            if self._document_kind(path) is None:
                continue
            documents[path] = self._read_document(snapshot_sha, path)
        return self._snapshot_from_documents(snapshot_sha, documents)

    def _snapshot_from_documents(
        self,
        snapshot_sha: str,
        documents: dict[str, RemoteDocument],
    ) -> CommandSnapshot:
        manifests = tuple(
            documents[path]
            for path in sorted(documents)
            if MANIFEST_PATH_RE.fullmatch(path)
        )
        commands = tuple(
            documents[path]
            for path in sorted(documents)
            if COMMAND_PATH_RE.fullmatch(path)
        )
        return CommandSnapshot(
            snapshot_sha=snapshot_sha,
            manifests=manifests,
            commands=commands,
        )

    @staticmethod
    def _document_kind(path: str) -> str | None:
        try:
            validate_repo_relative_path(path)
        except BridgeError:
            return None
        if MANIFEST_PATH_RE.fullmatch(path):
            return "manifest"
        if COMMAND_PATH_RE.fullmatch(path):
            return "command"
        return None

    def _read_document(self, snapshot_sha: str, path: str) -> RemoteDocument:
        try:
            completed = self._git.run_bytes(["show", f"{snapshot_sha}:{path}"], check=False)
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise BridgeError(
                    BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                    f"Missing or unreadable document {path} at snapshot {snapshot_sha}: {detail}",
                )
            content = completed.stdout

            completed_log = self._git.run_bytes(
                ["log", "-1", "--format=%H%x00%cI", snapshot_sha, "--", path],
                check=True,
            )
            raw_log = completed_log.stdout.rstrip(b"\r\n")
            commit_sha_bytes, separator, committed_at_bytes = raw_log.partition(b"\x00")
            if not separator:
                raise BridgeError(
                    BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                    f"Missing commit metadata for document {path}",
                )
            document_commit_sha = commit_sha_bytes.decode("ascii", errors="strict")
            validate_sha40("document commit SHA", document_commit_sha)
            document_committed_at = _canonical_git_timestamp(
                committed_at_bytes.decode("utf-8", errors="strict")
            )

            return RemoteDocument(
                path=path,
                content=content,
                document_commit_sha=document_commit_sha,
                document_committed_at=document_committed_at,
            )
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Failed to read document {path}: {exc}",
            ) from exc
