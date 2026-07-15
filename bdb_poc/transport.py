from __future__ import annotations

from pathlib import Path

from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.protocol import (
    BridgeError,
    COMMAND_PATH_RE,
    MANIFEST_PATH_RE,
    validate_repo_relative_path,
)
from bdb_bridge.transport import CommandSnapshot, RemoteDocument

from .git_ops import Git


class GitCommandTransport:
    COMMANDS_REF = "origin/commands"

    def __init__(self, repo_path: Path) -> None:
        self._git = Git(repo_path)

    def fetch_snapshot(self) -> CommandSnapshot:
        try:
            snapshot_sha = self._git.run(["rev-parse", self.COMMANDS_REF]).stdout.strip()
        except BridgeError as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Unable to resolve commands ref: {exc}",
            ) from exc

        try:
            completed = self._git.run(
                ["ls-tree", "-r", "--name-only", snapshot_sha, "--", "sessions"],
                check=False,
            )
        except BridgeError as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Unable to list commands tree: {exc}",
            ) from exc

        if completed.returncode not in (0, 128):
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                completed.stderr.strip() or "Unable to list commands tree",
            )

        manifest_paths: list[str] = []
        command_paths: list[str] = []
        for path in completed.stdout.splitlines():
            if not path:
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

    def _read_document(self, snapshot_sha: str, path: str) -> RemoteDocument:
        try:
            content = self._git.run(["show", f"{snapshot_sha}:{path}"]).stdout
        except BridgeError as exc:
            if exc.code == BridgeErrorCode.MISSING_PROTOCOL_FILE.value:
                raise BridgeError(
                    BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                    f"Missing document {path} at snapshot {snapshot_sha}",
                ) from exc
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Unable to read document {path}: {exc}",
            ) from exc

        try:
            document_commit_sha = self._git.run(
                ["log", "-1", "--format=%H", snapshot_sha, "--", path]
            ).stdout.strip()
        except BridgeError as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Unable to resolve document commit for {path}: {exc}",
            ) from exc

        if not document_commit_sha:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Unable to resolve document commit for {path}",
            )

        return RemoteDocument(
            path=path,
            content=content,
            document_commit_sha=document_commit_sha,
        )
