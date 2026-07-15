from __future__ import annotations

import sqlite3
from typing import Callable, Type

from .migrations import map_sqlite_error
from .protocol import (
    BridgeError,
    parse_strict_utc_timestamp,
    sanitize_diagnostics,
    validate_base_sha,
    validate_session_id,
)
from .workspace_types import WorkspaceDisposition, WorkspaceLifecycleRecord, WorkspaceLifecycleState

FaultHook = Callable[[str], None]
_SELECT = """SELECT session_id, workspace_path, base_sha, expected_revision,
expected_state_hash, disposition, state, requested_at, started_at, completed_at,
last_error, created_at, updated_at FROM workspace_lifecycle WHERE session_id = ?"""


def _hash(value: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise BridgeError("invalid_payload", "state hash must be canonical sha256:<64 lowercase hex>")
    if any(ch not in "0123456789abcdef" for ch in value[7:]):
        raise BridgeError("invalid_payload", "state hash must use lowercase hexadecimal")
    return value


def _row(row: tuple[object, ...]) -> WorkspaceLifecycleRecord:
    try:
        record = WorkspaceLifecycleRecord(
            session_id=str(row[0]), workspace_path=str(row[1]), base_sha=validate_base_sha(str(row[2])),
            expected_revision=int(row[3]), expected_state_hash=_hash(str(row[4])),
            disposition=WorkspaceDisposition(str(row[5])), state=WorkspaceLifecycleState(str(row[6])),
            requested_at=None if row[7] is None else str(row[7]),
            started_at=None if row[8] is None else str(row[8]),
            completed_at=None if row[9] is None else str(row[9]),
            last_error=None if row[10] is None else str(row[10]),
            created_at=str(row[11]), updated_at=str(row[12]),
        )
        validate_session_id(record.session_id)
        if not record.workspace_path or record.expected_revision < 0:
            raise ValueError("invalid identity")
        for name in ("requested_at", "started_at", "completed_at", "created_at", "updated_at"):
            value = getattr(record, name)
            if value is not None:
                parse_strict_utc_timestamp(value, field=name)
        if record.last_error is not None and len(record.last_error) > 500:
            raise ValueError("oversized diagnostic")
        return record
    except (BridgeError, ValueError, TypeError) as exc:
        raise BridgeError("journal_corrupt", "Invalid workspace_lifecycle row") from exc


def get_workspace_lifecycle(journal: object, session_id: str) -> WorkspaceLifecycleRecord | None:
    journal._ensure_open()
    validate_session_id(session_id)
    try:
        row = journal._connection.execute(_SELECT, (session_id,)).fetchone()
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="workspace lifecycle read") from exc
    return None if row is None else _row(row)


def _identity(record: WorkspaceLifecycleRecord, path: str, base: str, revision: int, state_hash: str) -> None:
    if (record.workspace_path, record.base_sha, record.expected_revision, record.expected_state_hash) != (
        path, base, revision, state_hash
    ):
        raise BridgeError("journal_conflict", "Workspace lifecycle identity/revision/state hash conflict")


def _event(journal: object, session_id: str, event_type: str, state: WorkspaceLifecycleState, now: str) -> None:
    journal._append_event_in_transaction(
        session_id=session_id, event_type=event_type, payload={"state": state.value}, created_at=now
    )


def record_workspace_preserved(
    journal: object, *, session_id: str, workspace_path: str, base_sha: str,
    expected_revision: int, expected_state_hash: str, fault_hook: FaultHook | None = None,
) -> WorkspaceLifecycleRecord:
    journal._ensure_open(); validate_session_id(session_id)
    if not isinstance(workspace_path, str) or not workspace_path:
        raise BridgeError("invalid_payload", "workspace_path must be non-empty")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise BridgeError("invalid_payload", "expected_revision must be non-negative")
    base_sha = validate_base_sha(base_sha); expected_state_hash = _hash(expected_state_hash)
    now = journal._now_fn(); parse_strict_utc_timestamp(now, field="now")
    try:
        with journal._transaction():
            current = get_workspace_lifecycle(journal, session_id)
            if current is not None:
                _identity(current, workspace_path, base_sha, expected_revision, expected_state_hash)
                if current.state is WorkspaceLifecycleState.REMOVED:
                    raise BridgeError("journal_conflict", "Removed workspace cannot be preserved")
                if current.disposition is WorkspaceDisposition.PRESERVE and current.state is WorkspaceLifecycleState.PRESERVED and current.last_error is None:
                    return current
                journal._connection.execute(
                    """UPDATE workspace_lifecycle SET disposition='preserve', state='preserved',
                    requested_at=NULL, started_at=NULL, completed_at=NULL, last_error=NULL, updated_at=?
                    WHERE session_id=?""", (now, session_id),
                )
            else:
                journal._connection.execute(
                    """INSERT INTO workspace_lifecycle(session_id,workspace_path,base_sha,expected_revision,
                    expected_state_hash,disposition,state,requested_at,started_at,completed_at,last_error,created_at,updated_at)
                    VALUES(?,?,?,?,?,'preserve','preserved',NULL,NULL,NULL,NULL,?,?)""",
                    (session_id, workspace_path, base_sha, expected_revision, expected_state_hash, now, now),
                )
            if fault_hook: fault_hook("AFTER_LIFECYCLE_STATE_WRITE_BEFORE_EVENT")
            _event(journal, session_id, "workspace.preserved", WorkspaceLifecycleState.PRESERVED, now)
    except BridgeError: raise
    except sqlite3.Error as exc: raise map_sqlite_error(exc, context="record workspace preserved") from exc
    result = get_workspace_lifecycle(journal, session_id); assert result is not None; return result


def _state_change(
    journal: object, *, session_id: str, accepted: set[WorkspaceLifecycleState],
    new_state: WorkspaceLifecycleState, event_type: str, disposition: WorkspaceDisposition,
    fault_hook: FaultHook | None = None, diagnostic: str | None = None,
) -> WorkspaceLifecycleRecord:
    journal._ensure_open(); validate_session_id(session_id)
    now = journal._now_fn(); parse_strict_utc_timestamp(now, field="now")
    try:
        with journal._transaction():
            current = get_workspace_lifecycle(journal, session_id)
            if current is None: raise BridgeError("journal_conflict", "Workspace lifecycle row is missing")
            if current.state is WorkspaceLifecycleState.REMOVED: return current
            if current.state is new_state and (diagnostic is None or current.last_error == diagnostic): return current
            if current.state not in accepted:
                raise BridgeError("journal_conflict", f"Invalid workspace lifecycle transition {current.state.value} -> {new_state.value}")
            requested = now if new_state is WorkspaceLifecycleState.CLEANUP_REQUESTED else current.requested_at
            started = now if new_state is WorkspaceLifecycleState.REMOVING else current.started_at
            completed = now if new_state is WorkspaceLifecycleState.REMOVED else current.completed_at
            updated = journal._connection.execute(
                """UPDATE workspace_lifecycle SET disposition=?, state=?, requested_at=?, started_at=?,
                completed_at=?, last_error=?, updated_at=? WHERE session_id=? AND state=?""",
                (disposition.value, new_state.value, requested, started, completed, diagnostic, now,
                 session_id, current.state.value),
            )
            if updated.rowcount != 1:
                raise BridgeError("journal_conflict", "Workspace lifecycle state changed concurrently")
            if fault_hook: fault_hook("AFTER_LIFECYCLE_STATE_WRITE_BEFORE_EVENT")
            _event(journal, session_id, event_type, new_state, now)
    except BridgeError: raise
    except sqlite3.Error as exc: raise map_sqlite_error(exc, context=event_type) from exc
    result = get_workspace_lifecycle(journal, session_id); assert result is not None; return result


def request_workspace_cleanup(journal: object, *, session_id: str, expected_state: WorkspaceLifecycleState = WorkspaceLifecycleState.PRESERVED, fault_hook: FaultHook | None = None) -> WorkspaceLifecycleRecord:
    return _state_change(journal, session_id=session_id,
        accepted={expected_state, WorkspaceLifecycleState.BLOCKED}, new_state=WorkspaceLifecycleState.CLEANUP_REQUESTED,
        event_type="workspace.cleanup_requested", disposition=WorkspaceDisposition.CLEANUP, fault_hook=fault_hook)


def mark_workspace_cleanup_started(journal: object, *, session_id: str, fault_hook: FaultHook | None = None) -> WorkspaceLifecycleRecord:
    return _state_change(journal, session_id=session_id,
        accepted={WorkspaceLifecycleState.CLEANUP_REQUESTED}, new_state=WorkspaceLifecycleState.REMOVING,
        event_type="workspace.cleanup_started", disposition=WorkspaceDisposition.CLEANUP, fault_hook=fault_hook)


def mark_workspace_cleanup_completed(journal: object, *, session_id: str, fault_hook: FaultHook | None = None) -> WorkspaceLifecycleRecord:
    return _state_change(journal, session_id=session_id,
        accepted={WorkspaceLifecycleState.CLEANUP_REQUESTED, WorkspaceLifecycleState.REMOVING},
        new_state=WorkspaceLifecycleState.REMOVED, event_type="workspace.cleanup_completed",
        disposition=WorkspaceDisposition.CLEANUP, fault_hook=fault_hook)


def mark_workspace_cleanup_blocked(journal: object, *, session_id: str, diagnostic: str, fault_hook: FaultHook | None = None) -> WorkspaceLifecycleRecord:
    message = sanitize_diagnostics(diagnostic, limit=500) or "workspace cleanup blocked"
    return _state_change(journal, session_id=session_id,
        accepted={WorkspaceLifecycleState.PRESERVED, WorkspaceLifecycleState.CLEANUP_REQUESTED,
                  WorkspaceLifecycleState.REMOVING, WorkspaceLifecycleState.BLOCKED},
        new_state=WorkspaceLifecycleState.BLOCKED, event_type="workspace.cleanup_blocked",
        disposition=WorkspaceDisposition.PRESERVE, fault_hook=fault_hook, diagnostic=message)


def _install_preserve_wrapper(journal_cls: Type[object], name: str, command_arg: bool) -> None:
    original = getattr(journal_cls, name, None)
    if original is None or getattr(original, "_ghb07_wrapped", False): return
    def wrapped(self: object, *args: object, **kwargs: object):
        result = original(self, *args, **kwargs)
        session_id = ""
        if command_arg:
            command_id = str(kwargs.get("command_id") or (args[0] if args else ""))
            command = self.get_command(command_id) if command_id else None
            session_id = command.session_id if command else ""
        else:
            session_id = str(kwargs.get("session_id") or (args[0] if args else ""))
        workspace = self.get_workspace(session_id) if session_id else None
        if workspace is not None and self.get_workspace_lifecycle(session_id) is None:
            self.record_workspace_preserved(session_id=session_id, workspace_path=workspace.workspace_path,
                base_sha=workspace.base_sha, expected_revision=workspace.revision,
                expected_state_hash=workspace.state_hash)
        return result
    wrapped._ghb07_wrapped = True
    setattr(journal_cls, name, wrapped)


def install_journal_workspace_lifecycle_api(journal_cls: Type[object]) -> None:
    for name, fn in {
        "get_workspace_lifecycle": get_workspace_lifecycle,
        "record_workspace_preserved": record_workspace_preserved,
        "request_workspace_cleanup": request_workspace_cleanup,
        "mark_workspace_cleanup_started": mark_workspace_cleanup_started,
        "mark_workspace_cleanup_completed": mark_workspace_cleanup_completed,
        "mark_workspace_cleanup_blocked": mark_workspace_cleanup_blocked,
    }.items(): setattr(journal_cls, name, fn)
    _install_preserve_wrapper(journal_cls, "mark_workspace_recovery_blocked", False)
    _install_preserve_wrapper(journal_cls, "mark_result_collision", True)
