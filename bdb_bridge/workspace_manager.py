from __future__ import annotations

import os
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

from .models import BridgeErrorCode, WorkspaceRecord
from .protocol import BridgeError, validate_repo_relative_path, path_matches
import subprocess
from typing import Iterable

class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(
        self,
        args: Iterable[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", "-C", str(cwd or self.repo), *list(args)]
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise BridgeError(BridgeErrorCode.GIT_ERROR, f"git {' '.join(args)} failed: {detail}")
        return completed


class WorkspaceManager:
    def __init__(self, config: Any, session_id: str, base_sha: str, manifest_paths: list[str]) -> None:
        self.config = config
        self.session_id = session_id
        self.base_sha = base_sha
        self.source_git = Git(config.fixture_repo_path)
        self.path = (config.worktree_root / session_id).resolve()
        self.manifest_paths = tuple(manifest_paths)

    @property
    def git(self) -> Git:
        return Git(self.path)

    def is_allowed_path(self, relative: str) -> bool:
        try:
            normalized = validate_repo_relative_path(relative)
            if not path_matches(normalized, self.config.allowed_paths):
                return False
            if not path_matches(normalized, self.manifest_paths):
                return False
            candidate = self.path.joinpath(*PurePosixPath(normalized).parts)
            # Check for symlinks and directory escapes
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.path.resolve())
            current = candidate
            while current != self.path:
                if current.exists() and current.is_symlink():
                    return False
                current = current.parent
            return True
        except Exception:
            return False

    def resolve_allowed_path(self, relative: str) -> Path:
        normalized = validate_repo_relative_path(relative)
        if not path_matches(normalized, self.config.allowed_paths):
            raise BridgeError(BridgeErrorCode.POLICY_DENIED, f"Path is not allowed by local policy: {normalized}")
        if not path_matches(normalized, self.manifest_paths):
            raise BridgeError(BridgeErrorCode.SCOPE_VIOLATION, f"Path is outside manifest scope: {normalized}")
        candidate = self.path.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.path.resolve())
        except ValueError as exc:
            raise BridgeError(BridgeErrorCode.UNSAFE_PATH, f"Path escaped workspace: {normalized}") from exc
        current = candidate
        while current != self.path:
            if current.exists() and current.is_symlink():
                raise BridgeError(BridgeErrorCode.UNSAFE_PATH, f"Symlink is not allowed: {normalized}")
            current = current.parent
        return resolved

    def compute_state_hash(self) -> str:
        head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
        paths = self.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
        digest = hashlib.sha256()
        digest.update(b"bdb-poc-state-v1\0")
        digest.update(head.encode("ascii"))
        digest.update(b"\0")
        for relative in sorted(set(paths)):
            if not self.is_allowed_path(relative):
                continue
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

    def compute_state_hash_with_override(self, relative_path: str, planned_bytes: bytes) -> str:
        normalized_override = validate_repo_relative_path(relative_path)
        head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
        paths = self.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
        all_paths = set(paths)
        all_paths.add(normalized_override)

        digest = hashlib.sha256()
        digest.update(b"bdb-poc-state-v1\0")
        digest.update(head.encode("ascii"))
        digest.update(b"\0")
        for relative in sorted(all_paths):
            if not self.is_allowed_path(relative):
                continue
            normalized = validate_repo_relative_path(relative)
            digest.update(normalized.encode("utf-8"))
            digest.update(b"\0")
            if normalized == normalized_override:
                if planned_bytes is not None:
                    digest.update(hashlib.sha256(planned_bytes).digest())
                else:
                    digest.update(b"<missing>")
            else:
                file_path = self.resolve_allowed_path(normalized)
                if file_path.is_file():
                    digest.update(hashlib.sha256(file_path.read_bytes()).digest())
                else:
                    digest.update(b"<missing>")
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def is_source_git_clean(self) -> bool:
        res = self.source_git.run(["status", "--porcelain=v1"])
        return not res.stdout.strip()

    def ensure_workspace(self, journal: Any) -> WorkspaceRecord:
        existing = journal.get_workspace(self.session_id)
        if existing is not None:
            # 5.3 Reattach existing registered worktree
            if not self.path.exists():
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Workspace path does not exist: {self.path}")

            # Check root escape & symlink
            root = self.config.worktree_root.resolve()
            if self.path.parent != root:
                raise BridgeError(BridgeErrorCode.UNSAFE_WORKTREE_PATH, "Session worktree escaped configured root")

            # Check git HEAD & repo
            try:
                head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
                if head != self.base_sha:
                    raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Workspace HEAD mismatch: {head} != {self.base_sha}")
            except Exception as exc:
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Workspace git verification failed: {exc}") from exc

            return existing

        # 5.2 Crash after creation, before DB registration
        if self.path.exists():
            root = self.config.worktree_root.resolve()
            if self.path.parent != root:
                raise BridgeError(BridgeErrorCode.UNSAFE_WORKTREE_PATH, "Session worktree escaped configured root")

            # Source checkout clean check
            if not self.is_source_git_clean():
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Source checkout is dirty")

            try:
                head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
                if head != self.base_sha:
                    raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Workspace HEAD mismatch: {head} != {self.base_sha}")

                # Check for untracked / modified files in newly attached worktree
                paths = self.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
                if paths:
                    raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Physical worktree contains unknown modifications: {paths}")
            except BridgeError:
                raise
            except Exception as exc:
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Physical worktree git check failed: {exc}") from exc

            # If everything matches, register existing worktree with revision 0
            state_hash = self.compute_state_hash()
            return journal.register_workspace(
                session_id=self.session_id,
                workspace_path=str(self.path),
                base_sha=self.base_sha,
                revision=0,
                state_hash=state_hash
            )

        # Normal creation flow
        if not self.config.fixture_repo_path.joinpath(".git").exists():
            raise BridgeError(BridgeErrorCode.INVALID_FIXTURE_REPO, "Fixture repository is not initialized")
        if not self.is_source_git_clean():
            raise BridgeError(BridgeErrorCode.DIRTY_SOURCE_CHECKOUT, "Fixture source checkout must be clean")

        # Verify base_sha exists
        verify = self.source_git.run(["cat-file", "-e", f"{self.base_sha}^{{commit}}"], check=False)
        if verify.returncode != 0:
            raise BridgeError(BridgeErrorCode.UNKNOWN_BASE_SHA, "Manifest base_sha is unavailable locally")

        self.config.worktree_root.mkdir(parents=True, exist_ok=True)
        root = self.config.worktree_root.resolve()
        if self.path.parent != root:
            raise BridgeError(BridgeErrorCode.UNSAFE_WORKTREE_PATH, "Session worktree escaped configured root")

        # Create worktree
        self.source_git.run(["worktree", "add", "--detach", str(self.path), self.base_sha])

        # Verify HEAD matches and source git is clean
        head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
        if head != self.base_sha:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Worktree HEAD mismatch after creation: {head} != {self.base_sha}")
        if not self.is_source_git_clean():
            raise BridgeError(BridgeErrorCode.DIRTY_SOURCE_CHECKOUT, "Source checkout dirty after worktree addition")

        state_hash = self.compute_state_hash()
        return journal.register_workspace(
            session_id=self.session_id,
            workspace_path=str(self.path),
            base_sha=self.base_sha,
            revision=0,
            state_hash=state_hash
        )

    def attach_existing_workspace(self, journal: Any) -> WorkspaceRecord:
        return self.ensure_workspace(journal)

    def read_exact_bytes(self, relative_path: str) -> bytes:
        path = self.resolve_allowed_path(relative_path)
        if not path.is_file():
            raise BridgeError(BridgeErrorCode.MISSING_FILE, f"File does not exist: {relative_path}")
        return path.read_bytes()

    def write_planned_bytes(self, relative_path: str, content: bytes) -> None:
        path = self.resolve_allowed_path(relative_path)
        dir_path = path.parent
        temp_name = f".bdb_temp_{path.name}"
        temp_path = dir_path / temp_name
        try:
            temp_path.write_bytes(content)
            fd = os.open(temp_path, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp_path, path)
        except Exception as exc:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, f"Failed to write file atomically: {exc}") from exc

    def verify_workspace(self, expected_revision: int, expected_state_hash: str) -> None:
        # Check actual workspace HEAD is base_sha
        try:
            head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
            if head != self.base_sha:
                raise BridgeError(BridgeErrorCode.WORKSPACE_DIVERGED, f"Workspace HEAD mismatch: {head} != {self.base_sha}")
        except Exception as exc:
            raise BridgeError(BridgeErrorCode.WORKSPACE_DIVERGED, f"Workspace HEAD check failed: {exc}") from exc

        # Check actual state hash matches
        actual_hash = self.compute_state_hash()
        if actual_hash != expected_state_hash:
            raise BridgeError(BridgeErrorCode.WORKSPACE_DIVERGED, f"Workspace state hash mismatch: {actual_hash} != {expected_state_hash}")

        # Check for untracked / modified paths that are not allowed by policy
        paths = self.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
        for p in paths:
            if not self.is_allowed_path(p):
                raise BridgeError(BridgeErrorCode.WORKSPACE_DIVERGED, f"Workspace has unauthorized untracked/modified path: {p}")

    def preserve_workspace(self) -> dict[str, Any]:
        # Return diagnostic summary
        try:
            head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
        except Exception as exc:
            head = f"error: {exc}"
        try:
            paths = self.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
        except Exception as exc:
            paths = [f"error: {exc}"]
        return {
            "path": str(self.path),
            "head": head,
            "modified_and_untracked": paths,
        }
