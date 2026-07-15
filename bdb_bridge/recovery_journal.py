from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import fields
from typing import Any, Callable

from .models import (
    BridgeErrorCode,
    CommandState,
    OperationEffectRecord,
    OperationPlanRecord,
    SessionState,
    TERMINAL_COMMAND_STATES,
    TERMINAL_SESSION_STATES,
    COMMAND_TRANSITIONS,
    validate_command_transition,
    validate_session_transition,
)
from .protocol import BridgeError
from .serializers import canonical_json


def sha256_bytes(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    return "sha256:" + hashlib.sha256(data).hexdigest()


FaultHook = Callable[[str], None]
_PLAN_MARKER = "bdb-operation-plan-v1"
_EFFECT_MARKER = "bdb-operation-effect-v1"


def operation_plan_payload(record: OperationPlanRecord) -> dict[str, object]:
    return {
        "schema": _PLAN_MARKER,
        "command_id": record.command_id,
        "session_id": record.session_id,
        "operation": record.operation,
        "target_path": record.target_path,
        "profile_id": record.profile_id,
        "expected_revision": record.expected_revision,
        "expected_state_hash": record.expected_state_hash,
        "workspace_revision_before": record.workspace_revision_before,
        "workspace_state_hash_before": record.workspace_state_hash_before,
        "before_content_sha256": record.before_content_sha256,
        "planned_after_content_sha256": record.planned_after_content_sha256,
        "planned_after_state_hash": record.planned_after_state_hash,
    }


def compute_operation_plan_sha256(record: OperationPlanRecord) -> str:
    return sha256_bytes(canonical_json(operation_plan_payload(record)).encode("utf-8"))


def operation_effect_payload(record: OperationEffectRecord) -> dict[str, object]:
    return {
        "schema": _EFFECT_MARKER,
        "command_id": record.command_id,
        "session_id": record.session_id,
        "plan_sha256": record.plan_sha256,
        "target_path": record.target_path,
        "workspace_revision_before": record.workspace_revision_before,
        "workspace_revision_after": record.workspace_revision_after,
        "workspace_state_hash_before": record.workspace_state_hash_before,
        "workspace_state_hash_after": record.workspace_state_hash_after,
        "before_content_sha256": record.before_content_sha256,
        "after_content_sha256": record.after_content_sha256,
    }


def compute_operation_effect_sha256(record: OperationEffectRecord) -> str:
    return sha256_bytes(canonical_json(operation_effect_payload(record)).encode("utf-8"))


def _validate_plan(record: OperationPlanRecord) -> None:
    if sha256_bytes(record.before_content) != record.before_content_sha256:
        raise BridgeError(BridgeErrorCode.OPERATION_PLAN_COLLISION, "before_content hash does not match exact bytes")
    if sha256_bytes(record.planned_after_content) != record.planned_after_content_sha256:
        raise BridgeError(BridgeErrorCode.OPERATION_PLAN_COLLISION, "planned_after_content hash does not match exact bytes")
    if record.plan_sha256 != compute_operation_plan_sha256(record):
        raise BridgeError(BridgeErrorCode.OPERATION_PLAN_COLLISION, "plan_sha256 does not match canonical immutable plan")


def _validate_effect(record: OperationEffectRecord) -> None:
    if record.workspace_revision_after != record.workspace_revision_before + 1:
        raise BridgeError(BridgeErrorCode.EFFECT_COLLISION, "effect revision must advance by exactly one")
    if record.effect_sha256 != compute_operation_effect_sha256(record):
        raise BridgeError(BridgeErrorCode.EFFECT_COLLISION, "effect_sha256 does not match canonical immutable effect")


def _plan_immutable(record: OperationPlanRecord) -> tuple[object, ...]:
    return tuple(getattr(record, field.name) for field in fields(OperationPlanRecord) if field.name != "created_at")


def _effect_immutable(record: OperationEffectRecord) -> tuple[object, ...]:
    return tuple(getattr(record, field.name) for field in fields(OperationEffectRecord) if field.name != "recorded_at")


def _sanitize_diagnostic(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "<bounded>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        return {str(k)[:80]: _sanitize_diagnostic(v, depth=depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_sanitize_diagnostic(v, depth=depth + 1) for v in list(value)[:20]]
    return str(value)[:500]


def _transition_session_in_transaction(journal: Any, session_id: str, current: SessionState, new: SessionState, now: str) -> None:
    validate_session_transition(current, new)
    updated = journal._connection.execute(
        "UPDATE sessions SET state = ?, updated_at = ? WHERE session_id = ? AND state = ?",
        (new.value, now, session_id, current.value),
    )
    if updated.rowcount != 1:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Failed to transition session {session_id}")
    journal._append_event_in_transaction(
        session_id=session_id,
        event_type="session.state_changed",
        payload={"from_state": current.value, "to_state": new.value},
        created_at=now,
    )


def mark_workspace_recovery_blocked(
    self: Any,
    *,
    session_id: str,
    command_id: str,
    reason_code: str,
    diagnostic: dict[str, object],
    fault_hook: FaultHook | None = None,
) -> None:
    self._ensure_open()
    now = self._now_fn()
    with self._transaction():
        command = self.get_command(command_id)
        session = self.get_session(session_id)
        if command is None or command.session_id != session_id:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command {command_id} does not belong to session {session_id}")
        if session is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Session {session_id} not found")
        if command.state not in TERMINAL_COMMAND_STATES:
            self._transition_command_in_transaction(
                command_id=command_id,
                expected_state=command.state,
                new_state=CommandState.MANUAL_RECONCILIATION_REQUIRED,
                now=now,
            )
        if fault_hook:
            fault_hook("AFTER_MANUAL_COMMAND_TRANSITION")
        if session.state not in TERMINAL_SESSION_STATES:
            _transition_session_in_transaction(
                self,
                session_id,
                session.state,
                SessionState.MANUAL_RECONCILIATION_REQUIRED,
                now,
            )
        if fault_hook:
            fault_hook("BEFORE_MANUAL_EVENT")
        exists = self._connection.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND command_id = ? AND event_type = 'workspace.recovery_blocked' LIMIT 1",
            (session_id, command_id),
        ).fetchone()
        if exists is None:
            self._append_event_in_transaction(
                session_id=session_id,
                command_id=command_id,
                event_type="workspace.recovery_blocked",
                payload={
                    "reason_code": str(reason_code)[:120],
                    "diagnostic": _sanitize_diagnostic(diagnostic),
                },
                created_at=now,
            )


def record_operation_plan(self: Any, record: OperationPlanRecord) -> None:
    self._ensure_open()
    _validate_plan(record)
    now = self._now_fn()
    with self._transaction():
        existing = self.get_operation_plan(record.command_id)
        if existing is not None:
            _validate_plan(existing)
            if _plan_immutable(existing) != _plan_immutable(record):
                raise BridgeError(BridgeErrorCode.OPERATION_PLAN_COLLISION, f"Different immutable plan already registered for command {record.command_id}")
            command = self.get_command(record.command_id)
            if command is None or command.state not in (CommandState.EXECUTING, CommandState.EFFECT_RECORDED):
                raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Replayed plan has inconsistent command state")
            return
        command = self.get_command(record.command_id)
        workspace = self.get_workspace(record.session_id)
        if command is None or command.session_id != record.session_id:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Plan command/session mismatch")
        if command.state != CommandState.CLAIMED:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, f"Plan requires CLAIMED command, got {command.state.value}")
        if workspace is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Plan workspace is missing")
        if workspace.revision != record.workspace_revision_before or workspace.state_hash != record.workspace_state_hash_before:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Plan workspace before-state does not match journal")
        self._connection.execute(
            """
            INSERT INTO operation_plans (
                command_id, session_id, operation, target_path, profile_id,
                expected_revision, expected_state_hash,
                workspace_revision_before, workspace_state_hash_before,
                before_content, before_content_sha256,
                planned_after_content, planned_after_content_sha256, planned_after_state_hash,
                plan_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.command_id, record.session_id, record.operation, record.target_path, record.profile_id,
                record.expected_revision, record.expected_state_hash,
                record.workspace_revision_before, record.workspace_state_hash_before,
                sqlite3.Binary(record.before_content), record.before_content_sha256,
                sqlite3.Binary(record.planned_after_content), record.planned_after_content_sha256,
                record.planned_after_state_hash, record.plan_sha256, now,
            ),
        )
        self._transition_command_in_transaction(
            command_id=record.command_id,
            expected_state=CommandState.CLAIMED,
            new_state=CommandState.EXECUTING,
            now=now,
        )
        self._append_event_in_transaction(
            session_id=record.session_id,
            command_id=record.command_id,
            event_type="operation.plan_recorded",
            payload={"target_path": record.target_path, "plan_sha256": record.plan_sha256},
            created_at=now,
        )


def record_operation_effect(
    self: Any,
    record: OperationEffectRecord,
    *,
    fault_hook: FaultHook | None = None,
) -> None:
    self._ensure_open()
    _validate_effect(record)
    now = self._now_fn()
    with self._transaction():
        plan = self.get_operation_plan(record.command_id)
        if plan is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Operation plan is missing")
        _validate_plan(plan)
        if record.plan_sha256 != plan.plan_sha256:
            raise BridgeError(BridgeErrorCode.EFFECT_COLLISION, "Effect plan hash does not match persisted plan")
        expected_fields = (
            record.session_id == plan.session_id,
            record.target_path == plan.target_path,
            record.workspace_revision_before == plan.workspace_revision_before,
            record.workspace_revision_after == plan.workspace_revision_before + 1,
            record.workspace_state_hash_before == plan.workspace_state_hash_before,
            record.workspace_state_hash_after == plan.planned_after_state_hash,
            record.before_content_sha256 == plan.before_content_sha256,
            record.after_content_sha256 == plan.planned_after_content_sha256,
        )
        if not all(expected_fields):
            raise BridgeError(BridgeErrorCode.EFFECT_COLLISION, "Effect immutable fields do not match persisted plan")
        existing = self.get_operation_effect(record.command_id)
        if existing is not None:
            _validate_effect(existing)
            if _effect_immutable(existing) != _effect_immutable(record):
                raise BridgeError(BridgeErrorCode.EFFECT_COLLISION, f"Different immutable effect already registered for command {record.command_id}")
            command = self.get_command(record.command_id)
            workspace = self.get_workspace(record.session_id)
            if command is None or command.state != CommandState.EFFECT_RECORDED:
                raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Replayed effect has inconsistent command state")
            if workspace is None or workspace.revision != record.workspace_revision_after or workspace.state_hash != record.workspace_state_hash_after:
                raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Replayed effect has inconsistent workspace after-state")
            return
        command = self.get_command(record.command_id)
        if command is None or command.state != CommandState.EXECUTING:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "New effect requires EXECUTING command")
        updated = self._connection.execute(
            """
            UPDATE workspaces SET revision = ?, state_hash = ?, updated_at = ?
            WHERE session_id = ? AND revision = ? AND state_hash = ?
            """,
            (
                record.workspace_revision_after,
                record.workspace_state_hash_after,
                now,
                record.session_id,
                record.workspace_revision_before,
                record.workspace_state_hash_before,
            ),
        )
        if updated.rowcount != 1:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Workspace state CAS failed")
        if fault_hook:
            fault_hook("AFTER_WORKSPACE_CAS")
        self._connection.execute(
            """
            INSERT INTO operation_effects (
                command_id, session_id, plan_sha256, target_path,
                workspace_revision_before, workspace_revision_after,
                workspace_state_hash_before, workspace_state_hash_after,
                before_content_sha256, after_content_sha256, effect_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.command_id, record.session_id, record.plan_sha256, record.target_path,
                record.workspace_revision_before, record.workspace_revision_after,
                record.workspace_state_hash_before, record.workspace_state_hash_after,
                record.before_content_sha256, record.after_content_sha256,
                record.effect_sha256, now,
            ),
        )
        if fault_hook:
            fault_hook("AFTER_EFFECT_INSERT")
        self._transition_command_in_transaction(
            command_id=record.command_id,
            expected_state=CommandState.EXECUTING,
            new_state=CommandState.EFFECT_RECORDED,
            now=now,
        )
        if fault_hook:
            fault_hook("BEFORE_EFFECT_EVENT")
        self._append_event_in_transaction(
            session_id=record.session_id,
            command_id=record.command_id,
            event_type="operation.effect_recorded",
            payload={"target_path": record.target_path, "effect_sha256": record.effect_sha256},
            created_at=now,
        )


def install_journal_recovery_api(journal_type: type[Any]) -> None:
    COMMAND_TRANSITIONS[CommandState.CLAIMED] = frozenset(
        set(COMMAND_TRANSITIONS[CommandState.CLAIMED])
        | {
            CommandState.STALE_REVISION,
            CommandState.STATE_MISMATCH,
            CommandState.POLICY_DENIED,
            CommandState.MANUAL_RECONCILIATION_REQUIRED,
        }
    )
    journal_type.mark_workspace_recovery_blocked = mark_workspace_recovery_blocked
    journal_type.record_operation_plan = record_operation_plan
    journal_type.record_operation_effect = record_operation_effect
