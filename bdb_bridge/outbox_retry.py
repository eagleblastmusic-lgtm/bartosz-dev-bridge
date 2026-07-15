from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .migrations import map_sqlite_error
from .models import BridgeErrorCode, OutboxRecord, OutboxState
from .protocol import BridgeError, parse_strict_utc_timestamp, validate_strict_utc_timestamp
from .outbox_common import (
    _get_outbox_row,
    _row_to_outbox,
    _sanitize_text,
    get_outbox,
)


def list_due_outbox(self: Any, now: str, limit: int = 100) -> list[OutboxRecord]:
    self._ensure_open()
    validate_strict_utc_timestamp(now, field="now")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 1000:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "limit must be an integer from 1 to 1000")
    try:
        rows = self._connection.execute(
            """
            SELECT command_id, session_id, sequence, result_sha256, remote_path,
                   state, attempt_count, next_attempt_at, last_error,
                   published_commit_sha, published_at, created_at, updated_at
            FROM outbox
            WHERE state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at ASC, session_id ASC, sequence ASC, command_id ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="list due outbox") from exc
    return [_row_to_outbox(row) for row in rows]


def _reservation_until(now: str, lease_seconds: float) -> str:
    if lease_seconds <= 0 or lease_seconds > 3600:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "lease_seconds must be in (0, 3600]")
    return (parse_strict_utc_timestamp(now, field="now") + timedelta(seconds=lease_seconds)).isoformat().replace(
        "+00:00", "Z"
    )


def claim_due_outbox(self: Any, now: str, *, lease_seconds: float = 60.0) -> OutboxRecord | None:
    self._ensure_open()
    validate_strict_utc_timestamp(now, field="now")
    reservation = _reservation_until(now, lease_seconds)
    with self._transaction():
        row = self._connection.execute(
            """
            SELECT command_id, session_id, sequence, result_sha256, remote_path,
                   state, attempt_count, next_attempt_at, last_error,
                   published_commit_sha, published_at, created_at, updated_at
            FROM outbox
            WHERE state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at ASC, session_id ASC, sequence ASC, command_id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        claimed = self._connection.execute(
            """
            UPDATE outbox SET next_attempt_at = ?, updated_at = ?
            WHERE command_id = ? AND state = 'pending' AND attempt_count = ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """,
            (reservation, now, row[0], row[6], now),
        )
        if claimed.rowcount != 1:
            return None
    return get_outbox(self, row[0])


def claim_outbox_command(
    self: Any,
    command_id: str,
    now: str,
    *,
    lease_seconds: float = 60.0,
) -> OutboxRecord | None:
    self._ensure_open()
    validate_strict_utc_timestamp(now, field="now")
    reservation = _reservation_until(now, lease_seconds)
    with self._transaction():
        row = _get_outbox_row(self, command_id)
        if row is None:
            return None
        current = _row_to_outbox(row)
        if current.state != OutboxState.PENDING:
            return current
        if current.next_attempt_at is not None and current.next_attempt_at > now:
            return None
        claimed = self._connection.execute(
            """
            UPDATE outbox SET next_attempt_at = ?, updated_at = ?
            WHERE command_id = ? AND state = 'pending' AND attempt_count = ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """,
            (reservation, now, command_id, current.attempt_count, now),
        )
        if claimed.rowcount != 1:
            return None
    return get_outbox(self, command_id)


def record_outbox_failure(
    self: Any,
    command_id: str,
    *,
    expected_attempt_count: int,
    error_message: str,
    now: str | None = None,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> OutboxRecord:
    self._ensure_open()
    if isinstance(expected_attempt_count, bool) or not isinstance(expected_attempt_count, int) or expected_attempt_count < 0:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "expected_attempt_count must be non-negative")
    if base_delay <= 0 or max_delay <= 0 or base_delay > max_delay:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "invalid retry delay bounds")
    current_time = now or self._now_fn()
    parsed_now = parse_strict_utc_timestamp(current_time, field="now")
    delay = min(max_delay, base_delay * (2 ** expected_attempt_count))
    next_attempt_at = (parsed_now + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
    sanitized = _sanitize_text(error_message)

    with self._transaction():
        row = _get_outbox_row(self, command_id)
        if row is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Outbox entry not found: {command_id}")
        record = _row_to_outbox(row)
        if record.state != OutboxState.PENDING:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Only pending outbox entries can record failure")
        if record.attempt_count == expected_attempt_count + 1:
            if record.last_error == sanitized and record.next_attempt_at == next_attempt_at:
                return record
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Outbox failure replay does not match persisted attempt")
        if record.attempt_count != expected_attempt_count:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Outbox attempt count CAS failed")
        updated = self._connection.execute(
            """
            UPDATE outbox
            SET attempt_count = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
            WHERE command_id = ? AND state = 'pending' AND attempt_count = ?
            """,
            (
                expected_attempt_count + 1,
                next_attempt_at,
                sanitized,
                current_time,
                command_id,
                expected_attempt_count,
            ),
        )
        if updated.rowcount != 1:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Outbox attempt count CAS failed")
        self._append_event_in_transaction(
            session_id=record.session_id,
            command_id=command_id,
            event_type="outbox.attempt_failed",
            payload={
                "attempt_count": expected_attempt_count + 1,
                "next_attempt_at": next_attempt_at,
                "error": sanitized,
            },
            created_at=current_time,
        )
    result = get_outbox(self, command_id)
    assert result is not None
    return result
