from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .protocol import BridgeError, sanitize_diagnostics
from .repository_index_models import GIT_COMMAND_TIMEOUT_SECONDS, FileKind


@dataclass(frozen=True)
class GitTreeEntry:
    mode: str
    object_type: str
    object_sha: str
    size_bytes: int
    path: str
    file_kind: FileKind


class GitObjectReader:
    """Read immutable Git objects without mutating the repository."""

    def __init__(self, repo_path: Path, *, timeout_seconds: int = GIT_COMMAND_TIMEOUT_SECONDS) -> None:
        self._repo_path = Path(repo_path)
        self._timeout_seconds = timeout_seconds

    def ensure_repository(self) -> None:
        if not self._repo_path.exists():
            raise BridgeError("invalid_config", "fixture_repo_path does not exist")
        result = self._run(["rev-parse", "--is-inside-work-tree"], check=False)
        if result.returncode != 0 or result.stdout.strip() not in {b"true", b"true\n"}:
            # bare repos also work for read-only indexing
            bare = self._run(["rev-parse", "--is-bare-repository"], check=False)
            if bare.returncode != 0 or bare.stdout.strip() not in {b"true", b"true\n"}:
                raise BridgeError("invalid_config", "fixture_repo_path is not a Git repository")

    def resolve_commit(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref or "\x00" in ref:
            raise BridgeError("invalid_payload", "Git ref must be a non-empty string")
        result = self._run(["rev-parse", "--verify", f"{ref}^{{commit}}"])
        sha = result.stdout.strip().decode("ascii", errors="strict")
        if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
            raise BridgeError("invalid_payload", "Resolved commit SHA is invalid")
        return sha

    def resolve_tree(self, commit_sha: str) -> str:
        result = self._run(["rev-parse", "--verify", f"{commit_sha}^{{tree}}"])
        sha = result.stdout.strip().decode("ascii", errors="strict")
        if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
            raise BridgeError("invalid_payload", "Resolved tree SHA is invalid")
        return sha

    def list_tree(self, commit_sha: str) -> tuple[GitTreeEntry, ...]:
        result = self._run(["ls-tree", "-r", "-z", "--long", commit_sha])
        raw = result.stdout
        if not raw:
            return ()
        entries: list[GitTreeEntry] = []
        for chunk in raw.split(b"\0"):
            if not chunk:
                continue
            try:
                meta, path_bytes = chunk.split(b"\t", 1)
            except ValueError as exc:
                raise BridgeError("invalid_payload", "Malformed git ls-tree entry") from exc
            parts = meta.split(b" ", 3)
            if len(parts) != 4:
                raise BridgeError("invalid_payload", "Malformed git ls-tree metadata")
            mode = parts[0].decode("ascii", errors="strict")
            object_type = parts[1].decode("ascii", errors="strict")
            object_sha = parts[2].decode("ascii", errors="strict").lower()
            size_token = parts[3].decode("ascii", errors="strict").strip()
            path = path_bytes.decode("utf-8", errors="strict")
            if "\x00" in path or path.startswith("/") or "\\" in path or any(
                part in {"", ".", ".."} for part in path.split("/")
            ):
                raise BridgeError("unsafe_path", f"Rejected unsafe git path: {sanitize_diagnostics(path)}")
            if len(object_sha) != 40 or any(ch not in "0123456789abcdef" for ch in object_sha):
                raise BridgeError("invalid_payload", "Invalid object SHA in ls-tree")
            file_kind = _file_kind(mode, object_type)
            if size_token == "-":
                size_bytes = 0
            else:
                try:
                    size_bytes = int(size_token)
                except ValueError as exc:
                    raise BridgeError("invalid_payload", "Invalid ls-tree size") from exc
                if size_bytes < 0:
                    raise BridgeError("invalid_payload", "Negative ls-tree size")
            entries.append(
                GitTreeEntry(
                    mode=mode,
                    object_type=object_type,
                    object_sha=object_sha,
                    size_bytes=size_bytes,
                    path=path,
                    file_kind=file_kind,
                )
            )
        entries.sort(key=lambda item: item.path)
        return tuple(entries)

    def read_blob(self, object_sha: str) -> bytes:
        result = self._run(["cat-file", "blob", object_sha])
        return result.stdout

    def content_sha256(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self._repo_path), *args],
                shell=False,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise BridgeError("invalid_config", "git executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise BridgeError("invalid_payload", f"git command timed out: {' '.join(args[:2])}") from exc
        if check and completed.returncode != 0:
            detail = sanitize_diagnostics(completed.stderr or completed.stdout) or "git command failed"
            raise BridgeError("invalid_payload", f"git {' '.join(args[:2])} failed: {detail}")
        return completed


def _file_kind(mode: str, object_type: str) -> FileKind:
    if mode == "120000":
        return FileKind.SYMLINK
    if mode == "160000" or object_type == "commit":
        return FileKind.SUBMODULE
    if object_type != "blob":
        raise BridgeError("invalid_payload", f"Unsupported git object type for file entry: {object_type}")
    return FileKind.REGULAR
