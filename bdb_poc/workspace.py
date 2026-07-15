from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    BridgeError,
    MAX_READ_LINES,
    changed_paths,
    path_matches,
    require_string,
    sanitized_test_environment,
    text_or_empty,
    validate_repo_relative_path,
)
from .config import BridgeConfig
from .git_ops import Git


class Workspace:
    def __init__(self, config: BridgeConfig, session_id: str, base_sha: str, manifest_paths: list[str]) -> None:
        self.config = config
        self.session_id = session_id
        self.base_sha = base_sha
        self.source_git = Git(config.fixture_repo_path)
        self.path = (config.worktree_root / session_id).resolve()
        self.revision = 0
        self.manifest_paths = tuple(manifest_paths)

    def create(self) -> None:
        if not self.config.fixture_repo_path.joinpath(".git").exists():
            raise BridgeError("invalid_fixture_repo", "Fixture repository is not initialized")
        if self.source_git.run(["status", "--porcelain=v1"]).stdout.strip():
            raise BridgeError("dirty_source_checkout", "Fixture source checkout must be clean")
        verify = self.source_git.run(["cat-file", "-e", f"{self.base_sha}^{{commit}}"], check=False)
        if verify.returncode != 0:
            raise BridgeError("unknown_base_sha", "Manifest base_sha is unavailable locally")
        self.config.worktree_root.mkdir(parents=True, exist_ok=True)
        root = self.config.worktree_root.resolve()
        if self.path.parent != root:
            raise BridgeError("unsafe_worktree_path", "Session worktree escaped configured root")
        if self.path.exists():
            raise BridgeError("workspace_exists", f"Workspace already exists: {self.path}")
        self.source_git.run(["worktree", "add", "--detach", str(self.path), self.base_sha])

    @property
    def git(self) -> Git:
        return Git(self.path)

    def resolve_allowed_path(self, relative: str) -> Path:
        normalized = validate_repo_relative_path(relative)
        if not path_matches(normalized, self.config.allowed_paths):
            raise BridgeError("policy_denied", f"Path is not allowed by local policy: {normalized}")
        if not path_matches(normalized, self.manifest_paths):
            raise BridgeError("scope_violation", f"Path is outside manifest scope: {normalized}")
        candidate = self.path.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.path.resolve())
        except ValueError as exc:
            raise BridgeError("unsafe_path", f"Path escaped workspace: {normalized}") from exc
        current = candidate
        while current != self.path:
            if current.exists() and current.is_symlink():
                raise BridgeError("unsafe_path", f"Symlink is not allowed: {normalized}")
            current = current.parent
        return resolved

    def state_hash(self) -> str:
        head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
        paths = self.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
        digest = hashlib.sha256()
        digest.update(b"bdb-poc-state-v1\0")
        digest.update(head.encode("ascii"))
        digest.update(b"\0")
        for relative in sorted(set(paths)):
            normalized = validate_repo_relative_path(relative)
            file_path = self.resolve_allowed_path(normalized)
            digest.update(normalized.encode("utf-8"))
            digest.update(b"\0")
            if file_path.is_file():
                digest.update(hashlib.sha256(file_path.read_bytes()).digest())
            else:
                digest.update(b"<missing>")
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def read_range(self, relative: str, start_line: int, end_line: int) -> dict[str, Any]:
        if start_line < 1 or end_line < start_line or end_line - start_line + 1 > MAX_READ_LINES:
            raise BridgeError("invalid_range", f"Read range must contain 1-{MAX_READ_LINES} lines")
        path = self.resolve_allowed_path(relative)
        if not path.is_file():
            raise BridgeError("missing_file", f"File does not exist: {relative}")
        lines = path.read_text(encoding="utf-8").splitlines()
        selected = lines[start_line - 1 : end_line]
        return {
            "path": relative,
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "content": "\n".join(selected),
        }

    def replace_exact_and_test(self, payload: dict[str, Any], python_executable: str, timeout: float) -> dict[str, Any]:
        if payload.get("profile_id") != "poc_pytest":
            raise BridgeError("policy_denied", "Only profile_id=poc_pytest is allowed")
        relative = require_string(payload, "path")
        old = require_string(payload, "old", allow_empty=False)
        new = require_string(payload, "new", allow_empty=True)
        path = self.resolve_allowed_path(relative)
        if not path.is_file():
            raise BridgeError("missing_file", f"File does not exist: {relative}")
        original = path.read_text(encoding="utf-8")
        count = original.count(old)
        if count != 1:
            raise BridgeError("replace_mismatch", f"Expected exactly one match, found {count}")
        path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
        revision_before = self.revision
        self.revision += 1

        started = time.monotonic()
        try:
            completed = subprocess.run(
                [python_executable, "-m", "pytest", "-q"],
                cwd=self.path,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=sanitized_test_environment(),
            )
            status = "success" if completed.returncode == 0 else "failed"
            exit_code: int | None = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            exit_code = None
            stdout = text_or_empty(exc.stdout)
            stderr = text_or_empty(exc.stderr)
        duration_ms = int((time.monotonic() - started) * 1000)
        diff = self.git.run(["diff", "--", relative]).stdout
        return {
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "revision_before": revision_before,
            "revision_after": self.revision,
            "changed_files": changed_paths(self.git.run(["status", "--porcelain=v1"]).stdout),
            "diff": diff,
        }
