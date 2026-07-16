from __future__ import annotations

import sqlite3
from typing import Any, Type

from .migrations import map_sqlite_error
from .models import BridgeErrorCode, CommandState, ProfileRunOutcome
from .multi_file_patch_recovery_models import MultiFileCheckpointState
from .multi_file_patch_runtime_models import MultiFilePatchProfileRecord
from .protocol import (
    BridgeError,
    parse_strict_utc_timestamp,
    validate_strict_utc_timestamp,
)
from .recovery_journal import sha256_bytes
from .serializers import MAX_TAIL_CHARS, tail


_PROFILE_SELECT = """SELECT command_id, profile_id, status, exit_code,
       stdout_tail, stderr_tail, stdout_sha256, stderr_sha256,
       duration_ms, started_at, finished_at, created_at
FROM multi_file_patch_profile_runs"""
_ALLOWED_STATUSES = frozenset({"success", "failed", "timeout", "internal_error"})


def _strict_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field} must be a string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{field} must be strict UTF-8",
        ) from exc
    return value


def _row_to_profile(row: sqlite3.Row | tuple[Any, ...]) -> MultiFilePatchProfileRecord:
    try:
        status = str(row[2])
        if status not in _ALLOWED_STATUSES:
            raise ValueError("invalid profile status")
        exit_code = row[3]
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise ValueError("invalid profile exit code")
        stdout_tail = str(row[4])
        stderr_tail = str(row[5])
        if len(stdout_tail) > MAX_TAIL_CHARS or len(stderr_tail) > MAX_TAIL_CHARS:
            raise ValueError("oversized profile output")
        for digest in (row[6], row[7]):
            if (
                not isinstance(digest, str)
                or len(digest) != 71
                or not digest.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in digest[7:])
            ):
                raise ValueError("invalid profile output hash")
        duration_ms = row[8]
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError("invalid profile duration")
        started_at = validate_strict_utc_timestamp(str(row[9]), field="profile.started_at")
        finished_at = validate_strict_utc_timestamp(str(row[10]), field="profile.finished_at")
        created_at = validate_strict_utc_timestamp(str(row[11]), field="profile.created_at")
        if parse_strict_utc_timestamp(finished_at, field="profile.finished_at") < parse_strict_utc_timestamp(
            started_at,
            field="profile.started_at",
        ):
            raise ValueError("profile finished before it started")
        return MultiFilePatchProfileRecord(
            command_id=str(row[0]),
            profile_id=str(row[1]),
            status=status,
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            stdout_sha256=str(row[6]),
            stderr_sha256=str(row[7]),
            duration_ms=duration_ms,
            started_at=started_at,
            finished_at=finished_at,
            created_at=created_at,
        )
    except BridgeError as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Invalid durable multi-file profile outcome",
        ) from exc
    except (IndexError, TypeError, ValueError) as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Invalid durable multi-file profile outcome",
        ) from exc


def get_multi_file_patch_profile_run(
    self: Any,
    command_id: str,
) -> MultiFilePatchProfileRecord | None:
    self._ensure_open()
    try:
        row = self._connection.execute(
            _PROFILE_SELECT + " WHERE command_id = ?",
            (command_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="multi-file profile outcome read") from exc
    return None if row is None else _row_to_profile(row)


def record_multi_file_patch_profile_run(
    self: Any,
    *,
    command_id: str,
    profile_id: str,
    outcome: ProfileRunOutcome,
    started_at: str,
    finished_at: str,
) -> MultiFilePatchProfileRecord:
    self._ensure_open()
    if outcome.status not in _ALLOWED_STATUSES:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"Unsupported profile status: {outcome.status!r}",
        )
    profile_id = _strict_text(profile_id, "profile_id")
    if not profile_id or len(profile_id) > 80:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "profile_id is not bounded")
    stdout = _strict_text(outcome.stdout, "profile.stdout")
    stderr = _strict_text(outcome.stderr, "profile.stderr")
    if outcome.exit_code is not None and (
        isinstance(outcome.exit_code, bool) or not isinstance(outcome.exit_code, int)
    ):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "profile exit_code is invalid")
    if isinstance(outcome.duration_ms, bool) or not isinstance(outcome.duration_ms, int) or outcome.duration_ms < 0:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "profile duration is invalid")
    started_at = validate_strict_utc_timestamp(started_at, field="profile.started_at")
    finished_at = validate_strict_utc_timestamp(finished_at, field="profile.finished_at")
    if parse_strict_utc_timestamp(finished_at, field="profile.finished_at") < parse_strict_utc_timestamp(
        started_at,
        field="profile.started_at",
    ):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "profile finished before it started")
    stdout_tail = tail(stdout, MAX_TAIL_CHARS)
    stderr_tail = tail(stderr, MAX_TAIL_CHARS)
    immutable = (
        command_id,
        profile_id,
        outcome.status,
        outcome.exit_code,
        stdout_tail,
        stderr_tail,
        sha256_bytes(stdout.encode("utf-8", errors="strict")),
        sha256_bytes(stderr.encode("utf-8", errors="strict")),
        outcome.duration_ms,
        started_at,
        finished_at,
    )
    existing = get_multi_file_patch_profile_run(self, command_id)
    if existing is not None:
        current = (
            existing.command_id,
            existing.profile_id,
            existing.status,
            existing.exit_code,
            existing.stdout_tail,
            existing.stderr_tail,
            existing.stdout_sha256,
            existing.stderr_sha256,
            existing.duration_ms,
            existing.started_at,
            existing.finished_at,
        )
        if current != immutable:
            raise BridgeError(
                BridgeErrorCode.EFFECT_COLLISION,
                "Different immutable profile outcome already exists",
            )
        return existing

    command = self.get_command(command_id)
    checkpoint = self.get_multi_file_patch_checkpoint(command_id)
    if command is None or command.state is not CommandState.EXECUTING:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            "New multi-file profile outcome requires EXECUTING command",
        )
    if checkpoint is None or checkpoint.state is not MultiFileCheckpointState.APPLIED:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            "Profile outcome requires an applied multi-file checkpoint",
        )
    now = self._now_fn()
    with self._transaction():
        self._connection.execute(
            """INSERT INTO multi_file_patch_profile_runs (
              command_id, profile_id, status, exit_code,
              stdout_tail, stderr_tail, stdout_sha256, stderr_sha256,
              duration_ms, started_at, finished_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*immutable, now),
        )
        self._append_event_in_transaction(
            session_id=command.session_id,
            command_id=command_id,
            event_type="multi_file_patch.profile_recorded",
            payload={
                "profile_id": profile_id,
                "status": outcome.status,
                "duration_ms": outcome.duration_ms,
            },
            created_at=now,
        )
    record = get_multi_file_patch_profile_run(self, command_id)
    assert record is not None
    return record


def mark_multi_file_patch_command_executing(self: Any, command_id: str) -> None:
    self._ensure_open()
    command = self.get_command(command_id)
    checkpoint = self.get_multi_file_patch_checkpoint(command_id)
    if command is None or checkpoint is None:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CONFLICT,
            "Command and checkpoint are required before execution",
        )
    if checkpoint.session_id != command.session_id:
        raise BridgeError(BridgeErrorCode.SESSION_MISMATCH, "Checkpoint command/session mismatch")
    if command.state is CommandState.EXECUTING:
        return
    if command.state is not CommandState.CLAIMED:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Checkpoint execution requires CLAIMED/EXECUTING, got {command.state.value}",
        )
    self.transition_command(command_id, CommandState.CLAIMED, CommandState.EXECUTING)


def finalize_multi_file_patch_execution(self: Any, command_id: str) -> None:
    """Atomically bind a terminal checkpoint outcome to EFFECT_RECORDED."""

    self._ensure_open()
    now = self._now_fn()
    with self._transaction():
        command = self.get_command(command_id)
        checkpoint = self.get_multi_file_patch_checkpoint(command_id)
        profile = get_multi_file_patch_profile_run(self, command_id)
        if command is None or checkpoint is None or profile is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Command, checkpoint and profile outcome are required",
            )
        workspace = self.get_workspace(command.session_id)
        if workspace is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint workspace is missing")
        success = profile.status == "success"
        expected_checkpoint = (
            MultiFileCheckpointState.COMMITTED
            if success
            else MultiFileCheckpointState.ROLLED_BACK
        )
        if command.state is CommandState.EFFECT_RECORDED:
            if checkpoint.state is not expected_checkpoint:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    "Recorded command/checkpoint terminal state mismatch",
                )
            return
        if command.state is not CommandState.EXECUTING:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"Final multi-file outcome requires EXECUTING, got {command.state.value}",
            )
        if success:
            if checkpoint.state is MultiFileCheckpointState.APPLIED:
                if (
                    workspace.revision != checkpoint.workspace_revision_before
                    or workspace.state_hash != checkpoint.workspace_state_hash_before
                ):
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_CONFLICT,
                        "Workspace before-state differs during final commit",
                    )
                updated_workspace = self._connection.execute(
                    """UPDATE workspaces SET revision = ?, state_hash = ?, updated_at = ?
                       WHERE session_id = ? AND revision = ? AND state_hash = ?""",
                    (
                        checkpoint.workspace_revision_before + 1,
                        checkpoint.workspace_state_hash_after,
                        now,
                        checkpoint.session_id,
                        checkpoint.workspace_revision_before,
                        checkpoint.workspace_state_hash_before,
                    ),
                )
                if updated_workspace.rowcount != 1:
                    raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Workspace commit CAS failed")
                updated_checkpoint = self._connection.execute(
                    """UPDATE multi_file_patch_checkpoints
                       SET state = 'committed', workspace_revision_after = ?, updated_at = ?
                       WHERE command_id = ? AND state = 'applied'""",
                    (checkpoint.workspace_revision_before + 1, now, command_id),
                )
                if updated_checkpoint.rowcount != 1:
                    raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint commit CAS failed")
            elif checkpoint.state is MultiFileCheckpointState.COMMITTED:
                if (
                    workspace.revision != checkpoint.workspace_revision_after
                    or workspace.state_hash != checkpoint.workspace_state_hash_after
                ):
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_CONFLICT,
                        "Committed checkpoint/workspace mismatch",
                    )
            else:
                raise BridgeError(
                    BridgeErrorCode.INVALID_STATE_TRANSITION,
                    "Successful profile requires APPLIED or COMMITTED checkpoint",
                )
        elif checkpoint.state is not MultiFileCheckpointState.ROLLED_BACK:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                "Failed profile must be fully rolled back before recording the outcome",
            )
        self._transition_command_in_transaction(
            command_id=command_id,
            expected_state=CommandState.EXECUTING,
            new_state=CommandState.EFFECT_RECORDED,
            now=now,
        )
        self._append_event_in_transaction(
            session_id=command.session_id,
            command_id=command_id,
            event_type="multi_file_patch.execution_recorded",
            payload={
                "checkpoint_state": expected_checkpoint.value,
                "profile_status": profile.status,
            },
            created_at=now,
        )


def install_journal_multi_file_patch_runtime_api(journal_cls: Type[object]) -> None:
    setattr(journal_cls, "get_multi_file_patch_profile_run", get_multi_file_patch_profile_run)
    setattr(journal_cls, "record_multi_file_patch_profile_run", record_multi_file_patch_profile_run)
    setattr(journal_cls, "mark_multi_file_patch_command_executing", mark_multi_file_patch_command_executing)
    setattr(journal_cls, "finalize_multi_file_patch_execution", finalize_multi_file_patch_execution)
