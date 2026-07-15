from __future__ import annotations

import errno
import hashlib
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .models import BridgeErrorCode, OperationPlanRecord, WorkspaceRecord
from .protocol import (
    BridgeError,
    path_matches,
    validate_base_sha,
    validate_repo_relative_path,
    validate_session_id,
)
from .recovery_journal import sha256_bytes


def changed_paths(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value.replace("\\", "/"))
    return sorted(paths)


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
        argv = ["git", "-C", str(cwd or self.repo), *list(args)]
        try:
            completed = subprocess.run(
                argv,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=timeout,
                env=env,
                shell=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, UnicodeError) as exc:
            raise BridgeError(BridgeErrorCode.GIT_ERROR, f"Controlled git failure for {list(args)!r}: {type(exc).__name__}") from exc
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:2_000]
            raise BridgeError(BridgeErrorCode.GIT_ERROR, f"git {' '.join(args)} failed: {detail}")
        return completed


class WorkspaceManager:
    def __init__(self, config: Any, session_id: str, base_sha: str, manifest_paths: list[str]) -> None:
        validate_session_id(session_id)
        self.config = config
        self.session_id = session_id
        self.base_sha = validate_base_sha(base_sha)
        self.source_git = Git(Path(config.fixture_repo_path))
        self.root = Path(config.worktree_root).expanduser().resolve(strict=False)
        self.path = self.root / session_id
        self.manifest_paths = tuple(manifest_paths)
        self._assert_expected_path()

    @property
    def git(self) -> Git:
        return Git(self.path)

    def _assert_expected_path(self) -> None:
        expected = self.root / self.session_id
        if self.path != expected or self.path.parent != self.root:
            raise BridgeError(BridgeErrorCode.UNSAFE_WORKTREE_PATH, "Workspace path is not exact <root>/<session_id>")
        self._assert_no_reparse_escape(self.root)
        self._assert_no_reparse_escape(self.path)

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))

    def _assert_no_reparse_escape(self, path: Path) -> None:
        absolute = path.absolute()
        parts = absolute.parts
        if not parts:
            raise BridgeError(BridgeErrorCode.UNSAFE_WORKTREE_PATH, "Empty workspace path")
        current = Path(parts[0])
        for part in parts[1:]:
            current /= part
            if current.exists() and self._is_reparse(current):
                raise BridgeError(BridgeErrorCode.UNSAFE_WORKTREE_PATH, f"Symlink/reparse component is not allowed: {current.name}")

    def is_allowed_path(self, relative: str) -> bool:
        try:
            normalized = validate_repo_relative_path(relative)
            return path_matches(normalized, self.config.allowed_paths) and path_matches(normalized, self.manifest_paths)
        except BridgeError:
            return False

    def resolve_allowed_path(self, relative: str) -> Path:
        normalized = validate_repo_relative_path(relative)
        if not path_matches(normalized, self.config.allowed_paths):
            raise BridgeError(BridgeErrorCode.POLICY_DENIED, f"Path is not allowed by local policy: {normalized}")
        if not path_matches(normalized, self.manifest_paths):
            raise BridgeError(BridgeErrorCode.SCOPE_VIOLATION, f"Path is outside manifest scope: {normalized}")
        candidate = self.path.joinpath(*PurePosixPath(normalized).parts)
        self._assert_no_reparse_escape(candidate)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.path.resolve(strict=False))
        except ValueError as exc:
            raise BridgeError(BridgeErrorCode.UNSAFE_PATH, f"Path escaped workspace: {normalized}") from exc
        return resolved

    def list_changed_paths(self) -> list[str]:
        return changed_paths(self.git.run(["status", "--porcelain=v1"]).stdout)

    def unauthorized_changed_paths(self, *, expected_temp: Path | None = None) -> list[str]:
        allowed_temp: str | None = None
        if expected_temp is not None:
            try:
                allowed_temp = expected_temp.relative_to(self.path).as_posix()
            except ValueError:
                allowed_temp = None
        return [path for path in self.list_changed_paths() if path != allowed_temp and not self.is_allowed_path(path)]

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
            digest.update(hashlib.sha256(file_path.read_bytes()).digest() if file_path.is_file() else b"<missing>")
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def compute_state_hash_with_override(self, relative_path: str, planned_bytes: bytes) -> str:
        normalized_override = validate_repo_relative_path(relative_path)
        self.resolve_allowed_path(normalized_override)
        head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
        paths = set(self.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines())
        paths.add(normalized_override)
        digest = hashlib.sha256()
        digest.update(b"bdb-poc-state-v1\0")
        digest.update(head.encode("ascii"))
        digest.update(b"\0")
        for relative in sorted(paths):
            if not self.is_allowed_path(relative):
                continue
            normalized = validate_repo_relative_path(relative)
            digest.update(normalized.encode("utf-8"))
            digest.update(b"\0")
            if normalized == normalized_override:
                digest.update(hashlib.sha256(planned_bytes).digest())
            else:
                file_path = self.resolve_allowed_path(normalized)
                digest.update(hashlib.sha256(file_path.read_bytes()).digest() if file_path.is_file() else b"<missing>")
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def is_source_git_clean(self) -> bool:
        return not self.source_git.run(["status", "--porcelain=v1"]).stdout.strip()

    def _worktree_entries(self) -> list[dict[str, object]]:
        output = self.source_git.run(["worktree", "list", "--porcelain"]).stdout
        entries: list[dict[str, object]] = []
        current: dict[str, object] = {}
        for line in output.splitlines() + [""]:
            if not line:
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key in {"detached", "bare", "locked", "prunable"}:
                current[key] = True
            else:
                current[key] = value
        return entries

    def _verify_worktree_registration(self) -> None:
        expected = self.path.resolve(strict=False)
        matching = []
        for entry in self._worktree_entries():
            raw = entry.get("worktree")
            if isinstance(raw, str) and Path(raw).resolve(strict=False) == expected:
                matching.append(entry)
        if len(matching) != 1:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Workspace is not exactly one worktree of the configured source repository")
        entry = matching[0]
        if not entry.get("detached"):
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Workspace HEAD must be detached")
        if str(entry.get("HEAD", "")).lower() != self.base_sha:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Worktree list HEAD does not match exact base SHA")
        head = self.git.run(["rev-parse", "HEAD"]).stdout.strip().lower()
        if head != self.base_sha:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Workspace HEAD does not match exact base SHA")
        symbolic = self.git.run(["symbolic-ref", "-q", "HEAD"], check=False)
        if symbolic.returncode == 0:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Workspace HEAD is attached to a branch")

    def ensure_workspace(self, journal: Any) -> WorkspaceRecord:
        self._assert_expected_path()
        if not Path(self.config.fixture_repo_path).joinpath(".git").exists():
            raise BridgeError(BridgeErrorCode.INVALID_FIXTURE_REPO, "Fixture repository is not initialized")
        if not self.is_source_git_clean():
            raise BridgeError(BridgeErrorCode.DIRTY_SOURCE_CHECKOUT, "Fixture source checkout must be clean")
        verify = self.source_git.run(["cat-file", "-e", f"{self.base_sha}^{{commit}}"], check=False)
        if verify.returncode != 0:
            raise BridgeError(BridgeErrorCode.UNKNOWN_BASE_SHA, "Manifest base_sha is unavailable locally")
        existing = journal.get_workspace(self.session_id)
        if existing is not None:
            if Path(existing.workspace_path) != self.path:
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Stored workspace path differs from exact expected path")
            if existing.base_sha.lower() != self.base_sha:
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Stored workspace base SHA differs from session base SHA")
            if not self.path.is_dir():
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Registered workspace path is missing")
            self._verify_worktree_registration()
            return existing
        if self.path.exists():
            if not self.path.is_dir():
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Expected workspace path is not a directory")
            self._verify_worktree_registration()
            if self.list_changed_paths():
                raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Orphan worktree is not clean")
            state_hash = self.compute_state_hash()
            return journal.register_workspace(
                session_id=self.session_id,
                workspace_path=str(self.path),
                base_sha=self.base_sha,
                revision=0,
                state_hash=state_hash,
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_no_reparse_escape(self.root)
        self.source_git.run(["worktree", "add", "--detach", str(self.path), self.base_sha])
        self._verify_worktree_registration()
        if not self.is_source_git_clean():
            raise BridgeError(BridgeErrorCode.DIRTY_SOURCE_CHECKOUT, "Source checkout dirty after worktree addition")
        state_hash = self.compute_state_hash()
        return journal.register_workspace(
            session_id=self.session_id,
            workspace_path=str(self.path),
            base_sha=self.base_sha,
            revision=0,
            state_hash=state_hash,
        )

    def validate_preplan_gate(
        self,
        record: WorkspaceRecord,
        *,
        expected_revision: int,
        expected_state_hash: str | None,
    ) -> str:
        if Path(record.workspace_path) != self.path or record.base_sha.lower() != self.base_sha:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Workspace registration identity mismatch")
        if not self.path.is_dir():
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Physical workspace is missing")
        self._verify_worktree_registration()
        if not self.is_source_git_clean():
            raise BridgeError(BridgeErrorCode.DIRTY_SOURCE_CHECKOUT, "Source checkout is dirty")
        foreign = self.unauthorized_changed_paths()
        if foreign:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Unauthorized workspace paths: {foreign[:20]}")
        actual = self.compute_state_hash()
        if actual != record.state_hash:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Physical workspace state differs from journal before plan")
        if expected_revision != record.revision:
            raise BridgeError(BridgeErrorCode.STALE_REVISION, f"Expected revision {expected_revision}, current revision is {record.revision}")
        if expected_state_hash is not None and expected_state_hash != record.state_hash:
            raise BridgeError(BridgeErrorCode.STATE_MISMATCH, "expected_state_hash does not match journal workspace")
        if expected_state_hash is not None and expected_state_hash != actual:
            raise BridgeError(BridgeErrorCode.STATE_MISMATCH, "expected_state_hash does not match physical workspace")
        return actual

    def read_exact_bytes(self, relative_path: str) -> bytes:
        path = self.resolve_allowed_path(relative_path)
        if not path.is_file():
            raise BridgeError(BridgeErrorCode.MISSING_FILE, f"File does not exist: {relative_path}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, f"Controlled target read failure: {type(exc).__name__}") from exc

    def temp_path_for(self, plan: OperationPlanRecord) -> Path:
        target = self.resolve_allowed_path(plan.target_path)
        suffix = plan.plan_sha256.removeprefix("sha256:")[:16]
        if len(suffix) != 16 or any(ch not in "0123456789abcdef" for ch in suffix):
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Invalid canonical plan hash for temp artifact")
        return target.parent / f".bdb_temp_{target.name}_{suffix}"

    def verify_expected_temp(self, plan: OperationPlanRecord) -> Path | None:
        temp = self.temp_path_for(plan)
        foreign = self.unauthorized_changed_paths(expected_temp=temp)
        if foreign:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, f"Foreign workspace paths: {foreign[:20]}")
        if not temp.exists():
            return None
        if not temp.is_file() or self._is_reparse(temp):
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Expected temp artifact is not a regular file")
        try:
            data = temp.read_bytes()
        except OSError as exc:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Expected temp artifact cannot be read") from exc
        if data != plan.planned_after_content or sha256_bytes(data) != plan.planned_after_content_sha256:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Expected temp artifact bytes differ from persisted plan")
        if self.read_exact_bytes(plan.target_path) != plan.before_content:
            raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, "Target is no longer in BEFORE state while temp exists")
        return temp

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.EBADF, errno.EPERM, errno.ENOTSUP}:
                    raise
        finally:
            os.close(fd)

    def apply_planned_bytes(
        self,
        plan: OperationPlanRecord,
        *,
        on_temp_written: Callable[[], None] | None = None,
    ) -> None:
        target = self.resolve_allowed_path(plan.target_path)
        temp = self.verify_expected_temp(plan)
        try:
            if temp is None:
                temp = self.temp_path_for(plan)
                with temp.open("xb") as stream:
                    stream.write(plan.planned_after_content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if temp.read_bytes() != plan.planned_after_content:
                    raise OSError("temp reread mismatch")
            if on_temp_written:
                on_temp_written()
            os.replace(temp, target)
            self._fsync_parent(target.parent)
            if target.read_bytes() != plan.planned_after_content:
                raise OSError("target reread mismatch")
        except OSError as exc:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, f"Controlled atomic write failure: {type(exc).__name__}") from exc

    def preserve_workspace(self) -> dict[str, Any]:
        head = "unavailable"
        paths: list[str] = []
        try:
            head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
        except BridgeError as exc:
            head = f"error:{exc.code}"
        try:
            paths = self.list_changed_paths()[:20]
        except BridgeError as exc:
            paths = [f"error:{exc.code}"]
        return {"workspace": self.path.name, "head": head, "modified_and_untracked": paths}
