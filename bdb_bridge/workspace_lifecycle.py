from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .migrations import map_sqlite_error
from .models import CommandState, OutboxState, ServiceInstanceState, SessionState
from .protocol import BridgeError, sanitize_diagnostics, validate_session_id
from .service_status import ServiceStatusReader
from .workspace_manager import WorkspaceManager
from .workspace_types import (
    WorkspaceCleanupOutcome,
    WorkspaceDisposition,
    WorkspaceEligibility,
    WorkspaceLifecycleState,
    WorkspaceStatusSnapshot,
)

FaultHook = Callable[[str], None]
_RECOVERABLE = frozenset(
    {
        CommandState.DISCOVERED.value,
        CommandState.VALIDATED.value,
        CommandState.CLAIMED.value,
        CommandState.EXECUTING.value,
        CommandState.EFFECT_RECORDED.value,
        CommandState.RESULT_STAGED.value,
    }
)


class WorkspaceLifecycleCoordinator:
    def __init__(self, config: Any, journal: Any, *, fault_hook: FaultHook | None = None) -> None:
        self.config = config
        self.journal = journal
        self.fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self.fault_hook:
            self.fault_hook(point)

    def _workspace_manager(self, session_id: str) -> WorkspaceManager:
        session = self.journal.get_session(session_id)
        if session is None:
            raise BridgeError("journal_conflict", f"Session not found: {session_id}")
        ingestion = self.journal.get_session_ingestion(session_id)
        if ingestion is None:
            raise BridgeError("journal_conflict", "Session ingestion manifest is missing")
        try:
            manifest = json.loads(ingestion.manifest_json)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise BridgeError("journal_corrupt", "Persisted manifest is invalid JSON") from exc
        allowed = manifest.get("allowed_paths") if isinstance(manifest, dict) else None
        if not isinstance(allowed, list) or not allowed or not all(isinstance(value, str) for value in allowed):
            raise BridgeError("journal_corrupt", "Persisted manifest allowed_paths is invalid")
        return WorkspaceManager(self.config, session_id, session.base_sha, allowed)

    @staticmethod
    def _has_reparse_component(path: Path) -> str | None:
        absolute = path.expanduser().absolute()
        if not absolute.parts:
            return "empty path"
        current = Path(absolute.parts[0])
        for part in absolute.parts[1:]:
            current /= part
            if current.exists() and WorkspaceManager._is_reparse(current):
                return current.name
        return None

    @staticmethod
    def _identity(entries: list[dict[str, object]]) -> dict[str, tuple[str, bool]]:
        return {
            str(Path(str(entry["worktree"])).resolve(strict=False)): (
                str(entry.get("HEAD", "")).lower(), bool(entry.get("detached"))
            )
            for entry in entries
            if isinstance(entry.get("worktree"), str)
        }

    def _registration_count(self, manager: WorkspaceManager) -> int:
        expected = manager.path.resolve(strict=False)
        return sum(
            1
            for entry in manager._worktree_entries()
            if isinstance(entry.get("worktree"), str)
            and Path(str(entry["worktree"])).resolve(strict=False) == expected
        )

    def _active_service(self) -> bool:
        row = self.journal._connection.execute(
            "SELECT 1 FROM service_instances WHERE state IN (?,?) LIMIT 1",
            (ServiceInstanceState.RUNNING.value, ServiceInstanceState.STOPPING.value),
        ).fetchone()
        return row is not None

    def _outbox_flags(self, session_id: str) -> tuple[bool, bool]:
        states = {
            str(row[0])
            for row in self.journal._connection.execute(
                "SELECT state FROM outbox WHERE session_id=? AND state IN ('pending','collision')",
                (session_id,),
            ).fetchall()
        }
        return OutboxState.PENDING.value in states, OutboxState.COLLISION.value in states

    def _recoverable_command(self, session_id: str) -> bool:
        placeholders = ",".join("?" for _ in _RECOVERABLE)
        row = self.journal._connection.execute(
            f"SELECT 1 FROM commands WHERE session_id=? AND state IN ({placeholders}) LIMIT 1",
            (session_id, *_RECOVERABLE),
        ).fetchone()
        return row is not None

    def assess_cleanup(self, session_id: str, *, lock_held: bool) -> WorkspaceEligibility:
        validate_session_id(session_id)
        try:
            reasons: list[str] = []
            session = self.journal.get_session(session_id)
            workspace = self.journal.get_workspace(session_id)
            lifecycle = self.journal.get_workspace_lifecycle(session_id)
            if session is None:
                return WorkspaceEligibility(False, ("session missing",))
            if session.state is not SessionState.COMPLETED:
                reasons.append(f"session state is {session.state.value}, not completed")
            if session.state is SessionState.MANUAL_RECONCILIATION_REQUIRED:
                reasons.append("manual reconciliation is preserve-only")
            if not lock_held:
                reasons.append("instance lock is not held")
            if self._active_service():
                reasons.append("service is running or stopping")
            if self._recoverable_command(session_id):
                reasons.append("recoverable command exists")
            pending, collision = self._outbox_flags(session_id)
            if pending:
                reasons.append("pending outbox exists")
            if collision:
                reasons.append("collision outbox exists")
            if self.journal._connection.execute(
                "SELECT 1 FROM ingestion_issues WHERE session_id=? AND blocking=1 LIMIT 1",
                (session_id,),
            ).fetchone() is not None:
                reasons.append("blocking ingestion issue exists")
            if workspace is None:
                reasons.append("workspace row missing")
                return WorkspaceEligibility(False, tuple(reasons))

            raw_root = Path(self.config.worktree_root).expanduser().absolute()
            raw_target = raw_root / session_id
            for candidate in (raw_root, raw_target):
                component = self._has_reparse_component(candidate)
                if component:
                    reasons.append(f"symlink/reparse component is not allowed: {component}")
            resolved_root = raw_root.resolve(strict=False)
            actual = Path(workspace.workspace_path).resolve(strict=False)
            if actual != resolved_root / session_id or actual.parent != resolved_root:
                reasons.append("workspace path is not exact <worktree_root>/<session_id>")
            if lifecycle is not None and (
                lifecycle.workspace_path != workspace.workspace_path
                or lifecycle.base_sha != workspace.base_sha.lower()
                or lifecycle.expected_revision != workspace.revision
                or lifecycle.expected_state_hash != workspace.state_hash
            ):
                reasons.append("workspace lifecycle identity mismatch")

            manager = self._workspace_manager(session_id)
            try:
                manager._assert_expected_path()
            except BridgeError as exc:
                reasons.append(sanitize_diagnostics(str(exc)))
            present = manager.path.exists()
            registrations = self._registration_count(manager)
            if not present:
                reasons.append("workspace path missing")
            if registrations != 1:
                reasons.append(f"worktree registration count is {registrations}, not one")
            if present and registrations == 1:
                try:
                    manager._verify_worktree_registration()
                except BridgeError as exc:
                    reasons.append(sanitize_diagnostics(str(exc)))
                try:
                    if not manager.is_source_git_clean():
                        reasons.append("source fixture repository is dirty")
                except BridgeError as exc:
                    reasons.append(sanitize_diagnostics(str(exc)))
                try:
                    changed = manager.list_changed_paths()
                    if any(Path(path).name.startswith(".bdb_temp_") for path in changed):
                        reasons.append("temporary workspace artifact exists")
                    if manager.unauthorized_changed_paths():
                        reasons.append("unauthorized workspace paths exist")
                    if manager.compute_state_hash() != workspace.state_hash:
                        reasons.append("physical workspace state hash differs from journal")
                except BridgeError as exc:
                    reasons.append(sanitize_diagnostics(str(exc)))
            return WorkspaceEligibility(not reasons, tuple(dict.fromkeys(reasons)))
        except sqlite3.Error as exc:
            raise map_sqlite_error(exc, context="workspace cleanup eligibility") from exc

    def preserve(self, session_id: str):
        validate_session_id(session_id)
        workspace = self.journal.get_workspace(session_id)
        if workspace is None:
            raise BridgeError("journal_conflict", f"Workspace not found for session {session_id}")
        return self.journal.record_workspace_preserved(
            session_id=session_id,
            workspace_path=workspace.workspace_path,
            base_sha=workspace.base_sha,
            expected_revision=workspace.revision,
            expected_state_hash=workspace.state_hash,
            fault_hook=self.fault_hook,
        )

    def status(self, session_id: str) -> WorkspaceStatusSnapshot:
        validate_session_id(session_id)
        try:
            session = self.journal.get_session(session_id)
            workspace = self.journal.get_workspace(session_id)
            lifecycle = self.journal.get_workspace_lifecycle(session_id)
            pending, collision = self._outbox_flags(session_id)
            service = ServiceStatusReader(self.config).get_status(self.journal)
            present = False
            registered = False
            physical_hash: str | None = None
            if session is not None and workspace is not None:
                manager = self._workspace_manager(session_id)
                present = manager.path.exists()
                registered = self._registration_count(manager) == 1
                if present:
                    physical_hash = manager.compute_state_hash()
            eligibility = self.assess_cleanup(session_id, lock_held=False)
            return WorkspaceStatusSnapshot(
                session_id=session_id,
                session_state=session.state.value if session else None,
                workspace_path=workspace.workspace_path if workspace else None,
                registered=workspace is not None,
                present=present,
                worktree_registered=registered,
                base_sha=workspace.base_sha if workspace else None,
                revision=workspace.revision if workspace else None,
                journal_state_hash=workspace.state_hash if workspace else None,
                physical_state_hash=physical_hash,
                disposition=lifecycle.disposition.value if lifecycle else WorkspaceDisposition.PRESERVE.value,
                lifecycle_state=lifecycle.state.value if lifecycle else WorkspaceLifecycleState.PRESERVED.value,
                eligible=eligibility.eligible,
                blocking_reasons=eligibility.reasons,
                pending_outbox=pending,
                collision_outbox=collision,
                recoverable_command=self._recoverable_command(session_id) if session else False,
                service_status=service.status.value,
                lock_held=service.lock_held,
            )
        except sqlite3.Error as exc:
            raise map_sqlite_error(exc, context="workspace status") from exc

    def cleanup(
        self, session_id: str, *, confirm_session_id: str, lock_held: bool
    ) -> WorkspaceCleanupOutcome:
        validate_session_id(session_id)
        if confirm_session_id != session_id:
            raise BridgeError("policy_denied", "--confirm-session-id must exactly match --session-id")
        if not lock_held:
            raise BridgeError("instance_lock_failed", "Workspace cleanup requires the shared instance lock")
        workspace = self.journal.get_workspace(session_id)
        if workspace is None:
            raise BridgeError("journal_conflict", f"Workspace not found for session {session_id}")
        lifecycle = self.journal.get_workspace_lifecycle(session_id) or self.preserve(session_id)
        if lifecycle.state is WorkspaceLifecycleState.REMOVED:
            return WorkspaceCleanupOutcome(session_id, lifecycle.state, False, True)

        manager = self._workspace_manager(session_id)
        present = manager.path.exists()
        registrations = self._registration_count(manager)
        if lifecycle.state in {
            WorkspaceLifecycleState.CLEANUP_REQUESTED,
            WorkspaceLifecycleState.REMOVING,
        }:
            if not present and registrations == 0:
                if not manager.is_source_git_clean():
                    return self._block(session_id, "source fixture repository is dirty after removal")
                completed = self.journal.mark_workspace_cleanup_completed(
                    session_id=session_id, fault_hook=self.fault_hook
                )
                return WorkspaceCleanupOutcome(session_id, completed.state, False, False)
            if present != (registrations > 0) or registrations != 1:
                return self._block(
                    session_id,
                    "workspace path/registration are inconsistent; cleanup recovery refuses prune or manual delete",
                )
        else:
            eligibility = self.assess_cleanup(session_id, lock_held=lock_held)
            if not eligibility.eligible:
                return self._block(session_id, "; ".join(eligibility.reasons))
            self.journal.request_workspace_cleanup(
                session_id=session_id, fault_hook=self.fault_hook
            )
            self._fault("AFTER_CLEANUP_REQUEST_BEFORE_START")

        eligibility = self.assess_cleanup(session_id, lock_held=lock_held)
        if not eligibility.eligible:
            return self._block(session_id, "; ".join(eligibility.reasons))
        lifecycle = self.journal.get_workspace_lifecycle(session_id)
        assert lifecycle is not None
        if lifecycle.state is WorkspaceLifecycleState.CLEANUP_REQUESTED:
            self.journal.mark_workspace_cleanup_started(
                session_id=session_id, fault_hook=self.fault_hook
            )
        self._fault("AFTER_CLEANUP_STARTED_BEFORE_REMOVE")

        before = self._identity(manager._worktree_entries())
        target = str(manager.path.resolve(strict=False))
        try:
            manager.source_git.run(["worktree", "remove", "--force", str(manager.path)])
        except BridgeError as exc:
            return self._block(session_id, f"git worktree remove failed: {exc}")
        self._fault("AFTER_WORKTREE_REMOVE_BEFORE_JOURNAL_ACK")
        after = self._identity(manager._worktree_entries())
        before.pop(target, None)
        after.pop(target, None)
        if before != after:
            return self._block(session_id, "unrelated worktree registration changed during cleanup")
        if manager.path.exists() or self._registration_count(manager) != 0:
            return self._block(session_id, "target worktree still exists after git worktree remove")
        if not manager.is_source_git_clean():
            return self._block(session_id, "source fixture repository became dirty during cleanup")
        completed = self.journal.mark_workspace_cleanup_completed(
            session_id=session_id, fault_hook=self.fault_hook
        )
        return WorkspaceCleanupOutcome(session_id, completed.state, True, False)

    def _block(self, session_id: str, diagnostic: str) -> WorkspaceCleanupOutcome:
        record = self.journal.mark_workspace_cleanup_blocked(
            session_id=session_id,
            diagnostic=diagnostic,
            fault_hook=self.fault_hook,
        )
        return WorkspaceCleanupOutcome(session_id, record.state, False, False, record.last_error)
