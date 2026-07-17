from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from .protocol import BridgeError, validate_repo_relative_path


_RESULT_PATH_RE = re.compile(
    r"^sessions/(?P<session>[^/]+)/results/(?P<sequence>[0-9]{6})\.json$"
)
_MAX_RESULT_BYTES = 2 * 1024 * 1024


class LocalResultSink:
    """Persist exact staged result bytes for the browser/native-host return path."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve(strict=False)

    def publish(self, remote_path: str, content: bytes) -> Path:
        validate_repo_relative_path(remote_path)
        if _RESULT_PATH_RE.fullmatch(remote_path) is None:
            raise BridgeError("unsafe_path", f"Not a canonical result path: {remote_path}")
        if not isinstance(content, bytes):
            raise BridgeError("invalid_payload", "Local result content must be bytes")
        if len(content) > _MAX_RESULT_BYTES:
            raise BridgeError("invalid_payload", "Local result exceeds the byte limit")

        relative = PurePosixPath(remote_path)
        destination = self.root_dir.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = destination.parent.resolve(strict=True)
        if self.root_dir != resolved_parent and self.root_dir not in resolved_parent.parents:
            raise BridgeError("unsafe_path", "Local result path escaped the result root")
        if destination.is_symlink():
            raise BridgeError("unsafe_path", "Local result destination must not be a symlink")
        if destination.exists():
            existing = destination.read_bytes()
            if existing == content:
                return destination
            raise BridgeError("journal_conflict", f"Local result collision at {remote_path}")

        temporary = destination.parent / f".{destination.name}.{os.getpid()}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            self._fsync_directory(destination.parent)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return destination

    def read(self, remote_path: str) -> bytes | None:
        validate_repo_relative_path(remote_path)
        if _RESULT_PATH_RE.fullmatch(remote_path) is None:
            raise BridgeError("unsafe_path", f"Not a canonical result path: {remote_path}")
        relative = PurePosixPath(remote_path)
        path = self.root_dir.joinpath(*relative.parts)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise BridgeError("unsafe_path", "Local result entry must be a regular file")
        data = path.read_bytes()
        if len(data) > _MAX_RESULT_BYTES:
            raise BridgeError("invalid_payload", "Local result exceeds the byte limit")
        return data

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
