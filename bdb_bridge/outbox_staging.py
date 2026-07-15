from __future__ import annotations

from typing import Any

from .models import BridgeErrorCode, CommandState, OutboxRecord, OutboxState, ResultRecord
from .protocol import BridgeError, validate_strict_utc_timestamp
from .recovery_journal import sha256_bytes
from .outbox_common import (
    FaultHook,
    _outbox_matches,
    _result_matches,
    _validate_staged_result,
    get_outbox,
)


def stage_result_and_enqueue(
    self: Any,
    *,
    command_id: str,
    result_json: str,
    remote_path: str,
    fault_hook: FaultHook | None = None,
) -> tuple[ResultRecord, OutboxRecord]:
    self._ensure_open()
    parsed, result_bytes, status, error_code, command, _plan, _effect = _validate_staged_result(
        self, command_id=command_id, result_json=result_json, remote_path=remote_path
    )
    result_sha256 = sha256_bytes(result_bytes)
    now = self._now_fn()
    validate_strict_utc_timestamp(now, field="now")

    with self._transaction():
        existing_result = self.get_result(command_id)
        existing_outbox = get_outbox(self, command_id)
        current = self.get_command(command_id)
        if current is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command not found: {command_id}")

        if existing_result is not None or existing_outbox is not None:
            if existing_result is None or existing_outbox is None:
                raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Result/outbox atomicity invariant is broken")
            if not _result_matches(
                existing_result,
                result_json=result_json,
                result_sha256=result_sha256,
                remote_path=remote_path,
                status=status,
                error_code=error_code,
            ) or not _outbox_matches(existing_outbox, existing_result):
                raise BridgeError(BridgeErrorCode.RESULT_COLLISION, f"Result collision for command {command_id}")
            if current.state not in {CommandState.RESULT_STAGED, CommandState.RESULT_PUBLISHED}:
                raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Replayed staged result has inconsistent command state")
            return existing_result, existing_outbox

        if current.state != CommandState.EFFECT_RECORDED:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"New staged result requires EFFECT_RECORDED, got {current.state.value}",
            )
        occupied = self._connection.execute(
            "SELECT command_id FROM results WHERE session_id = ? AND sequence = ?",
            (current.session_id, current.sequence),
        ).fetchone()
        path_occupied = self._connection.execute(
            "SELECT command_id FROM outbox WHERE remote_path = ?",
            (remote_path,),
        ).fetchone()
        legacy_path_occupied = self._connection.execute(
            "SELECT command_id FROM results WHERE remote_path = ? AND command_id != ?",
            (remote_path, command_id),
        ).fetchone()
        if occupied is not None or path_occupied is not None or legacy_path_occupied is not None:
            raise BridgeError(BridgeErrorCode.RESULT_COLLISION, "Result sequence or remote path is already occupied")

        self._connection.execute(
            """
            INSERT INTO results (
                command_id, session_id, sequence, status, error_code,
                result_sha256, result_json, remote_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                current.session_id,
                current.sequence,
                status,
                error_code,
                result_sha256,
                result_json,
                remote_path,
                now,
            ),
        )
        if fault_hook:
            fault_hook("AFTER_RESULT_INSERT")
        self._connection.execute(
            """
            INSERT INTO outbox (
                command_id, session_id, sequence, result_sha256, remote_path,
                state, attempt_count, next_attempt_at, last_error,
                published_commit_sha, published_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (
                command_id,
                current.session_id,
                current.sequence,
                result_sha256,
                remote_path,
                OutboxState.PENDING.value,
                now,
                now,
            ),
        )
        if fault_hook:
            fault_hook("AFTER_OUTBOX_INSERT")
            fault_hook("BEFORE_RESULT_STAGED_TRANSITION")
        self._transition_command_in_transaction(
            command_id=command_id,
            expected_state=CommandState.EFFECT_RECORDED,
            new_state=CommandState.RESULT_STAGED,
            now=now,
        )
        if fault_hook:
            fault_hook("BEFORE_STAGE_EVENTS")
        self._append_event_in_transaction(
            session_id=current.session_id,
            command_id=command_id,
            event_type="result.staged",
            payload={"sequence": current.sequence, "result_sha256": result_sha256},
            created_at=now,
        )
        self._append_event_in_transaction(
            session_id=current.session_id,
            command_id=command_id,
            event_type="outbox.enqueued",
            payload={"remote_path": remote_path},
            created_at=now,
        )

    result = self.get_result(command_id)
    outbox = get_outbox(self, command_id)
    assert result is not None and outbox is not None
    return result, outbox
