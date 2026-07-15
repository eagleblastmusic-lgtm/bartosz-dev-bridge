from __future__ import annotations

import re
import subprocess
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

    def fetch_snapshot(self) -> CommandSnapshot:
        try:
            # 1. Read-only fetch
            self._git.run_bytes(
                [
                    "fetch",
                    "--prune",
                    self._remote,
                    f"+refs/heads/{self._commands_branch}:refs/remotes/{self._remote}/{self._commands_branch}",
                ],
                check=True,
            )

            # 2. Resolve snapshot SHA
            ref = f"refs/remotes/{self._remote}/{self._commands_branch}"
            completed = self._git.run_bytes(["rev-parse", ref], check=True)
            snapshot_sha = completed.stdout.decode("utf-8", errors="strict").strip()
            validate_sha40("snapshot SHA", snapshot_sha)

            # 3. List tree with null-terminated output
            completed_tree = self._git.run_bytes(
                ["ls-tree", "-rz", "--name-only", snapshot_sha, "--", "sessions"],
                check=False,
            )

            # exit code 128 means path "sessions" doesn't exist yet, which is fine (empty snapshot)
            if completed_tree.returncode not in (0, 128):
                detail = completed_tree.stderr.decode("utf-8", errors="replace").strip()
                raise BridgeError(
                    BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                    f"ls-tree failed: {detail}",
                )

            stdout_bytes = completed_tree.stdout
            manifest_paths: list[str] = []
            command_paths: list[str] = []

            for path_bytes in stdout_bytes.split(b"\x00"):
                if not path_bytes:
                    continue
                try:
                    path = path_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    continue
                try:
                    validate_repo_relative_path(path)
                except BridgeError:
                    continue
                if MANIFEST_PATH_RE.fullmatch(path):
                    manifest_paths.append(path)
                elif COMMAND_PATH_RE.fullmatch(path):
                    command_paths.append(path)

            manifests = tuple(
                self._read_document(snapshot_sha, path) for path in sorted(manifest_paths)
            )
            commands = tuple(
                self._read_document(snapshot_sha, path) for path in sorted(command_paths)
            )

            return CommandSnapshot(
                snapshot_sha=snapshot_sha,
                manifests=manifests,
                commands=commands,
            )
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Git snapshot fetch failed: {exc}",
            ) from exc

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
                ["log", "-1", "--format=%H", snapshot_sha, "--", path],
                check=True,
            )
            document_commit_sha = completed_log.stdout.decode("utf-8", errors="strict").strip()
            validate_sha40("document commit SHA", document_commit_sha)

            return RemoteDocument(
                path=path,
                content=content,
                document_commit_sha=document_commit_sha,
            )
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Failed to read document {path}: {exc}",
            ) from exc
