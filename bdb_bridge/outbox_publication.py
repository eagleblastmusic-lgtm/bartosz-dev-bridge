from __future__ import annotations

from typing import Any

from .models import (
    BridgeErrorCode,
    CommandState,
    OutboxRecord,
    OutboxState,
    SessionState,
    validate_session_transition,
)
from .protocol import BridgeError, validate_strict_utc_timestamp
from .outbox_common import (
    FaultHook,
    _get_outbox_row,
    _require_commit_sha,
    _require_hash,
    _row_to_outbox,
    _sanitize_text,
    get_outbox,
)


def mark_result_published(
    self: Any,
    command_id: str,
    *,
    remote_result_sha256: str,
    published_commit_sha: str,
    published_at: str | None = None,
    fault_hook: FaultHook | None = None,
) -> OutboxRecord:
    self._ensure_open()
    remote_result_sha256 = _require_hash(remote_result_sha256, "remote_result_sha256")
    published_commit_sha = _require_commit_sha(published_commit_sha, "published_commit_sha")
    now = published_at or self._now_fn()
    validate_strict_utc_timestamp(now, field="published_at")

    with self._transaction():
        result = self.get_result(command_id)
        row = _get_outbox_row(self, command_id)
        command = self.get_command(command_id)
        if result is None or row is None or command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Command/result/outbox must exist before publish acknowledgement")
        outbox = _row_to_outbox(row)
        if result.result_sha256 != outbox.result_sha256 or result.result_sha256 != remote_result_sha256:
            raise BridgeError(BridgeErrorCode.RESULT_COLLISION, "Remote result hash differs from immutable staged result")
        if outbox.state == OutboxState.PUBLISHED:
            if command.state == CommandState.RESULT_PUBLISHED and outbox.published_commit_sha == published_commit_sha:
                return outbox
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Published outbox replay differs from persisted acknowledgement")
        if outbox.state != OutboxState.PENDING or command.state != CommandState.RESULT_STAGED:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Publish acknowledgement requires pending/RESULT_STAGED")
        self._connection.execute(
            """
            UPDATE outbox SET state = 'published', published_commit_sha = ?, published_at = ?,
                              next_attempt_at = NULL, last_error = NULL, updated_at = ?
            WHERE command_id = ? AND state = 'pending'
            """,
            (published_commit_sha, now, now, command_id),
        )
        if fault_hook:
            fault_hook("AFTER_OUTBOX_PUBLISHED_BEFORE_COMMAND_TRANSITION")
        self._transition_command_in_transaction(
            command_id=command_id,
            expected_state=CommandState.RESULT_STAGED,
            new_state=CommandState.RESULT_PUBLISHED,
            now=now,
        )
        if fault_hook:
            fault_hook("BEFORE_PUBLISHED_EVENT")
        exists = self._connection.execute(
            "SELECT 1 FROM events WHERE command_id = ? AND event_type = 'result.published' LIMIT 1",
            (command_id,),
        ).fetchone()
        if exists is None:
            self._append_event_in_transaction(
                session_id=outbox.session_id,
                command_id=command_id,
                event_type="result.published",
                payload={
                    "remote_path": outbox.remote_path,
                    "result_sha256": outbox.result_sha256,
                    "published_commit_sha": published_commit_sha,
                },
                created_at=now,
            )
    record = get_outbox(self, command_id)
    assert record is not None
    return record


def _transition_session_manual(journal: Any, session_id: str, current: SessionState, now: str) -> None:
    validate_session_transition(current, SessionState.MANUAL_RECONCILIATION_REQUIRED)
    updated = journal._connection.execute(
        "UPDATE sessions SET state = ?, updated_at = ? WHERE session_id = ? AND state = ?",
        (SessionState.MANUAL_RECONCILIATION_REQUIRED.value, now, session_id, current.value),
    )
    if updated.rowcount != 1:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Session manual-reconciliation CAS failed")
    journal._append_event_in_transaction(
        session_id=session_id,
        event_type="session.state_changed",
        payload={"from_state": current.value, "to_state": SessionState.MANUAL_RECONCILIATION_REQUIRED.value},
        created_at=now,
    )


def mark_result_collision(
    self: Any,
    command_id: str,
    *,
    observed_result_sha256: str,
    diagnostic: str,
    fault_hook: FaultHook | None = None,
) -> OutboxRecord:
    self._ensure_open()
    observed_result_sha256 = _require_hash(observed_result_sha256, "observed_result_sha256")
    now = self._now_fn()
    validate_strict_utc_timestamp(now, field="now")

    with self._transaction():
        result = self.get_result(command_id)
        row = _get_outbox_row(self, command_id)
        command = self.get_command(command_id)
        if result is None or row is None or command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Command/result/outbox must exist before collision")
        outbox = _row_to_outbox(row)
        session = self.get_session(outbox.session_id)
        if session is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Collision session is missing")
        if outbox.state == OutboxState.COLLISION:
            if command.state == CommandState.MANUAL_RECONCILIATION_REQUIRED and session.state == SessionState.MANUAL_RECONCILIATION_REQUIRED:
                return outbox
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Collision replay has inconsistent command/session state")
        if outbox.state != OutboxState.PENDING or command.state != CommandState.RESULT_STAGED:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Collision requires pending/RESULT_STAGED")
        detail = _sanitize_text(
            f"path={outbox.remote_path} expected={outbox.result_sha256} observed={observed_result_sha256} {diagnostic}"
        )
        self._connection.execute(
            """
            UPDATE outbox SET state = 'collision', next_attempt_at = NULL, last_error = ?, updated_at = ?
            WHERE command_id = ? AND state = 'pending'
            """,
            (detail, now, command_id),
        )
        if fault_hook:
            fault_hook("AFTER_COLLISION_OUTBOX_BEFORE_MANUAL_STATE")
        self._transition_command_in_transaction(
            command_id=command_id,
            expected_state=CommandState.RESULT_STAGED,
            new_state=CommandState.MANUAL_RECONCILIATION_REQUIRED,
            now=now,
        )
        if session.state in {SessionState.ACTIVE, SessionState.COMPLETING}:
            _transition_session_manual(self, session.session_id, session.state, now)
        elif session.state != SessionState.MANUAL_RECONCILIATION_REQUIRED:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Collision session must be active or completing")
        exists = self._connection.execute(
            "SELECT 1 FROM events WHERE command_id = ? AND event_type = 'result.collision' LIMIT 1",
            (command_id,),
        ).fetchone()
        if exists is None:
            self._append_event_in_transaction(
                session_id=outbox.session_id,
                command_id=command_id,
                event_type="result.collision",
                payload={
                    "remote_path": outbox.remote_path,
                    "expected_hash": outbox.result_sha256,
                    "observed_hash": observed_result_sha256,
                    "diagnostic": detail,
                },
                created_at=now,
            )
    record = get_outbox(self, command_id)
    assert record is not None
    return record
