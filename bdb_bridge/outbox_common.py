from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from typing import Any, Callable

from .migrations import map_sqlite_error
from .models import (
    BridgeErrorCode,
    CommandState,
    OutboxRecord,
    OutboxState,
    ResultRecord,
    ResultStatus,
    SessionState,
    validate_session_transition,
)
from .protocol import (
    BridgeError,
    SCHEMA_VERSION,
    parse_strict_utc_timestamp,
    result_path_for,
    validate_repo_relative_path,
    validate_strict_utc_timestamp,
)
from .recovery_journal import sha256_bytes
from .serializers import MAX_RESULT_BYTES, canonical_json

FaultHook = Callable[[str], None]
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_STATUSES = frozenset(member.value for member in ResultStatus)
_MAX_DIAGNOSTIC = 500


def _sanitize_text(value: object, *, limit: int = _MAX_DIAGNOSTIC) -> str:
    text = " ".join(str(value).replace("\x00", "").split())
    return text[:limit]


def _require_hash(value: str, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field} must be sha256:<64 lowercase hex>")
    return value


def _require_commit_sha(value: str, field: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field} must be a 40-character lowercase Git SHA")
    return value


def _row_to_outbox(row: tuple[Any, ...]) -> OutboxRecord:
    try:
        state = OutboxState(row[5])
        _require_hash(row[3], "outbox.result_sha256")
        validate_repo_relative_path(row[4])
        validate_strict_utc_timestamp(row[11], field="outbox.created_at")
        validate_strict_utc_timestamp(row[12], field="outbox.updated_at")
        if row[7] is not None:
            validate_strict_utc_timestamp(row[7], field="outbox.next_attempt_at")
        if row[10] is not None:
            validate_strict_utc_timestamp(row[10], field="outbox.published_at")
        if row[9] is not None:
            _require_commit_sha(row[9], "outbox.published_commit_sha")
        if type(row[2]) is not int or row[2] <= 0 or type(row[6]) is not int or row[6] < 0:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Invalid outbox sequence or attempt_count")
        if row[8] is not None and (not isinstance(row[8], str) or len(row[8]) > _MAX_DIAGNOSTIC):
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Invalid outbox last_error")
    except (ValueError, BridgeError) as exc:
        if isinstance(exc, BridgeError) and exc.code == BridgeErrorCode.JOURNAL_CORRUPT:
            raise
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, f"Invalid outbox record: {exc}") from exc
    return OutboxRecord(
        command_id=row[0],
        session_id=row[1],
        sequence=int(row[2]),
        result_sha256=row[3],
        remote_path=row[4],
        state=state,
        attempt_count=int(row[6]),
        next_attempt_at=row[7],
        last_error=row[8],
        published_commit_sha=row[9],
        published_at=row[10],
        created_at=row[11],
        updated_at=row[12],
    )


def _get_outbox_row(journal: Any, command_id: str) -> tuple[Any, ...] | None:
    return journal._connection.execute(
        """
        SELECT command_id, session_id, sequence, result_sha256, remote_path,
               state, attempt_count, next_attempt_at, last_error,
               published_commit_sha, published_at, created_at, updated_at
        FROM outbox WHERE command_id = ?
        """,
        (command_id,),
    ).fetchone()


def get_outbox(self: Any, command_id: str) -> OutboxRecord | None:
    self._ensure_open()
    if not isinstance(command_id, str) or not command_id:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "command_id must be a non-empty string")
    try:
        row = _get_outbox_row(self, command_id)
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="get outbox") from exc
    return None if row is None else _row_to_outbox(row)


def _validate_end_marker(parsed: dict[str, Any]) -> None:
    marker = parsed.get("end_marker")
    if not isinstance(marker, str):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result end_marker must be a string")
    without_marker = dict(parsed)
    without_marker.pop("end_marker", None)
    expected = "BDB-END:sha256:" + hashlib.sha256(
        canonical_json(without_marker).encode("utf-8", errors="strict")
    ).hexdigest()
    if marker != expected:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result end_marker does not match exact result payload")


def _validate_staged_result(
    journal: Any,
    *,
    command_id: str,
    result_json: str,
    remote_path: str,
) -> tuple[dict[str, Any], bytes, str, str | None, Any, Any, Any]:
    if not isinstance(result_json, str) or not result_json:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result_json must be a non-empty string")
    try:
        result_bytes = result_json.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result_json must be strict UTF-8") from exc
    if len(result_bytes) > MAX_RESULT_BYTES:
        raise BridgeError(BridgeErrorCode.RESULT_TOO_LARGE, f"Result exceeds {MAX_RESULT_BYTES} bytes")
    try:
        parsed = json.loads(result_json)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result_json must be a valid JSON object") from exc
    if not isinstance(parsed, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result_json must be a JSON object")

    command = journal.get_command(command_id)
    if command is None:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command not found: {command_id}")
    session = journal.get_session(command.session_id)
    plan = journal.get_operation_plan(command_id)
    effect = journal.get_operation_effect(command_id)
    if session is None or plan is None or effect is None:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Session, operation plan and effect must exist before staging")
    if command.state not in {
        CommandState.EFFECT_RECORDED,
        CommandState.RESULT_STAGED,
        CommandState.RESULT_PUBLISHED,
    }:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Result staging requires EFFECT_RECORDED/RESULT_STAGED/RESULT_PUBLISHED, got {command.state.value}",
        )
    if parsed.get("schema_version") != SCHEMA_VERSION:
        raise BridgeError(BridgeErrorCode.UNSUPPORTED_SCHEMA, "result schema_version must be 1.1")
    expected_fields = {
        "session_id": command.session_id,
        "command_id": command.command_id,
        "sequence": command.sequence,
        "workspace_revision_before": effect.workspace_revision_before,
        "workspace_revision_after": effect.workspace_revision_after,
        "state_hash_before": effect.workspace_state_hash_before,
        "state_hash_after": effect.workspace_state_hash_after,
    }
    for field, expected in expected_fields.items():
        if parsed.get(field) != expected:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"result {field} does not match persisted effect")
    if effect.session_id != command.session_id or effect.plan_sha256 != plan.plan_sha256:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Persisted plan/effect identity mismatch")

    status = parsed.get("status")
    if status not in _ALLOWED_STATUSES:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"Unsupported staged result status: {status!r}")
    error_code = parsed.get("error_code")
    if error_code is not None and (not isinstance(error_code, str) or not error_code):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result error_code must be null or a non-empty string")
    changed_files = parsed.get("changed_files")
    if not isinstance(changed_files, list) or not all(isinstance(item, str) for item in changed_files):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result changed_files must be a string list")
    if changed_files != sorted(set(changed_files)):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result changed_files must be sorted and unique")
    if parsed.get("artifacts") != []:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "GHB0-5 artifacts must be an empty list")
    for field in ("started_at", "finished_at"):
        validate_strict_utc_timestamp(parsed.get(field), field=f"result.{field}")
    started = parse_strict_utc_timestamp(parsed["started_at"], field="result.started_at")
    finished = parse_strict_utc_timestamp(parsed["finished_at"], field="result.finished_at")
    if finished < started or parsed.get("duration_ms") != int((finished - started).total_seconds() * 1000):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result duration does not match timestamps")
    if parsed.get("command_commit_sha") != command.command_commit_sha:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result command_commit_sha does not match command")
    if parsed.get("changed_files") != [effect.target_path]:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result changed_files must contain exactly the persisted target")
    for text_field, hash_field in (("stdout_tail", "stdout_sha256"), ("stderr_tail", "stderr_sha256"), ("diff", "diff_sha256")):
        text_value = parsed.get(text_field)
        if not isinstance(text_value, str):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"result {text_field} must be a string")
        try:
            encoded = text_value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"result {text_field} must be strict UTF-8") from exc
        hash_value = parsed.get(hash_field)
        _require_hash(hash_value, f"result.{hash_field}")
        if not parsed.get("truncated", False) and hash_value != sha256_bytes(encoded):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"result {hash_field} does not match exact bytes")
    if type(parsed.get("duration_ms")) is not int or parsed["duration_ms"] < 0:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result duration_ms must be non-negative")
    if parsed.get("exit_code") is not None and type(parsed.get("exit_code")) is not int:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result exit_code must be integer or null")
    if status == ResultStatus.SUCCESS.value and error_code is not None:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "successful result cannot carry error_code")
    _validate_end_marker(parsed)

    expected_path = result_path_for(command.session_id, command.sequence)
    validate_repo_relative_path(remote_path)
    if remote_path != expected_path:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"remote_path must be {expected_path}")
    return parsed, result_bytes, status, error_code, command, plan, effect


def _result_matches(
    record: ResultRecord,
    *,
    result_json: str,
    result_sha256: str,
    remote_path: str,
    status: str,
    error_code: str | None,
) -> bool:
    return (
        record.result_json == result_json
        and record.result_sha256 == result_sha256
        and record.remote_path == remote_path
        and record.status == status
        and record.error_code == error_code
    )


def _outbox_matches(record: OutboxRecord, result: ResultRecord) -> bool:
    return (
        record.command_id == result.command_id
        and record.session_id == result.session_id
        and record.sequence == result.sequence
        and record.result_sha256 == result.result_sha256
        and record.remote_path == result.remote_path
    )
