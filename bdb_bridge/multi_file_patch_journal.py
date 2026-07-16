from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Type

from .edit_operation_parser import sha256_bytes
from .models import BridgeErrorCode
from .multi_file_patch_recovery_models import (
    MultiFileCheckpointBundle,
    MultiFileCheckpointPath,
    MultiFileCheckpointRecord,
    MultiFileCheckpointState,
)
from .protocol import BridgeError
from .serializers import canonical_json


def _checkpoint_payload(
    *,
    command_id: str,
    session_id: str,
    patch_sha256: str,
    plan_sha256: str,
    workspace_revision_before: int,
    workspace_state_hash_before: str,
    workspace_state_hash_after: str,
    paths: tuple[MultiFileCheckpointPath, ...],
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "patch_sha256": patch_sha256,
        "paths": [
            {
                "after_exists": item.after_exists,
                "after_sha256": item.after_sha256,
                "before_exists": item.before_exists,
                "before_sha256": item.before_sha256,
                "operation_indices": list(item.operation_indices),
                "ordinal": item.ordinal,
                "path": item.path,
                "roles": list(item.roles),
            }
            for item in paths
        ],
        "plan_sha256": plan_sha256,
        "schema": "bdb-multi-file-checkpoint-v1",
        "session_id": session_id,
        "workspace_revision_before": workspace_revision_before,
        "workspace_state_hash_after": workspace_state_hash_after,
        "workspace_state_hash_before": workspace_state_hash_before,
    }


def compute_multi_file_checkpoint_sha256(
    *,
    command_id: str,
    session_id: str,
    patch_sha256: str,
    plan_sha256: str,
    workspace_revision_before: int,
    workspace_state_hash_before: str,
    workspace_state_hash_after: str,
    paths: tuple[MultiFileCheckpointPath, ...],
) -> str:
    payload = _checkpoint_payload(
        command_id=command_id,
        session_id=session_id,
        patch_sha256=patch_sha256,
        plan_sha256=plan_sha256,
        workspace_revision_before=workspace_revision_before,
        workspace_state_hash_before=workspace_state_hash_before,
        workspace_state_hash_after=workspace_state_hash_after,
        paths=paths,
    )
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def _validate_digest(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field} must be sha256:<64 lowercase hex>")


def _validate_paths(command_id: str, paths: tuple[MultiFileCheckpointPath, ...]) -> None:
    if not paths or len(paths) > 200:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint path count must be in 1..200")
    if tuple(item.ordinal for item in paths) != tuple(range(len(paths))):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint path ordinals are not contiguous")
    if tuple(item.path for item in paths) != tuple(sorted(item.path for item in paths)):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint paths are not sorted")
    if len({item.path for item in paths}) != len(paths):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint paths are not unique")
    for item in paths:
        if item.command_id != command_id:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint path command mismatch")
        if item.before_exists != (item.before is not None):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint before existence mismatch")
        if item.after_exists != (item.after is not None):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint after existence mismatch")
        before_hash = sha256_bytes(item.before) if item.before is not None else None
        after_hash = sha256_bytes(item.after) if item.after is not None else None
        if item.before_sha256 != before_hash or item.after_sha256 != after_hash:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint path bytes/hash mismatch")
        if item.before_exists == item.after_exists and item.before == item.after:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint path has no net change")
        if item.roles != tuple(sorted(set(item.roles))) or not item.roles:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint path roles are not canonical")
        if item.operation_indices != tuple(sorted(set(item.operation_indices))) or not item.operation_indices:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Checkpoint operation indices are not canonical")


def _row_to_record(row: sqlite3.Row | tuple[Any, ...]) -> MultiFileCheckpointRecord:
    return MultiFileCheckpointRecord(
        command_id=str(row[0]),
        session_id=str(row[1]),
        patch_sha256=str(row[2]),
        plan_sha256=str(row[3]),
        checkpoint_sha256=str(row[4]),
        state=MultiFileCheckpointState(str(row[5])),
        workspace_revision_before=int(row[6]),
        workspace_state_hash_before=str(row[7]),
        workspace_revision_after=int(row[8]) if row[8] is not None else None,
        workspace_state_hash_after=str(row[9]),
        path_count=int(row[10]),
        total_before_bytes=int(row[11]),
        total_after_bytes=int(row[12]),
        last_error=str(row[13]) if row[13] is not None else None,
        created_at=str(row[14]),
        updated_at=str(row[15]),
    )


def _row_to_path(row: sqlite3.Row | tuple[Any, ...]) -> MultiFileCheckpointPath:
    roles = tuple(json.loads(str(row[9])))
    indices = tuple(int(value) for value in json.loads(str(row[10])))
    return MultiFileCheckpointPath(
        command_id=str(row[0]),
        ordinal=int(row[1]),
        path=str(row[2]),
        before_exists=bool(row[3]),
        before=bytes(row[4]) if row[4] is not None else None,
        before_sha256=str(row[5]) if row[5] is not None else None,
        after_exists=bool(row[6]),
        after=bytes(row[7]) if row[7] is not None else None,
        after_sha256=str(row[8]) if row[8] is not None else None,
        roles=roles,
        operation_indices=indices,
    )


_RECORD_SELECT = """
SELECT command_id, session_id, patch_sha256, plan_sha256, checkpoint_sha256,
       state, workspace_revision_before, workspace_state_hash_before,
       workspace_revision_after, workspace_state_hash_after, path_count,
       total_before_bytes, total_after_bytes, last_error, created_at, updated_at
FROM multi_file_patch_checkpoints
"""

_PATH_SELECT = """
SELECT command_id, ordinal, path, before_exists, before_content, before_sha256,
       after_exists, after_content, after_sha256, roles_json, operation_indices_json
FROM multi_file_patch_checkpoint_paths
"""


def get_multi_file_patch_checkpoint(self: Any, command_id: str) -> MultiFileCheckpointRecord | None:
    self._ensure_open()
    row = self._connection.execute(_RECORD_SELECT + " WHERE command_id = ?", (command_id,)).fetchone()
    return _row_to_record(row) if row is not None else None


def list_multi_file_patch_checkpoint_paths(
    self: Any, command_id: str
) -> tuple[MultiFileCheckpointPath, ...]:
    self._ensure_open()
    rows = self._connection.execute(
        _PATH_SELECT + " WHERE command_id = ? ORDER BY ordinal ASC", (command_id,)
    ).fetchall()
    return tuple(_row_to_path(row) for row in rows)


def get_multi_file_patch_bundle(self: Any, command_id: str) -> MultiFileCheckpointBundle | None:
    record = get_multi_file_patch_checkpoint(self, command_id)
    if record is None:
        return None
    paths = list_multi_file_patch_checkpoint_paths(self, command_id)
    if len(paths) != record.path_count:
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Checkpoint path count differs from header")
    _validate_paths(command_id, paths)
    expected = compute_multi_file_checkpoint_sha256(
        command_id=record.command_id,
        session_id=record.session_id,
        patch_sha256=record.patch_sha256,
        plan_sha256=record.plan_sha256,
        workspace_revision_before=record.workspace_revision_before,
        workspace_state_hash_before=record.workspace_state_hash_before,
        workspace_state_hash_after=record.workspace_state_hash_after,
        paths=paths,
    )
    if expected != record.checkpoint_sha256:
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Checkpoint hash mismatch")
    return MultiFileCheckpointBundle(record=record, paths=paths)


def record_multi_file_patch_checkpoint(
    self: Any,
    *,
    command_id: str,
    session_id: str,
    patch_sha256: str,
    plan_sha256: str,
    checkpoint_sha256: str,
    workspace_revision_before: int,
    workspace_state_hash_before: str,
    workspace_state_hash_after: str,
    paths: Iterable[MultiFileCheckpointPath],
) -> MultiFileCheckpointRecord:
    self._ensure_open()
    canonical_paths = tuple(paths)
    _validate_digest(patch_sha256, "patch_sha256")
    _validate_digest(plan_sha256, "plan_sha256")
    _validate_digest(checkpoint_sha256, "checkpoint_sha256")
    _validate_digest(workspace_state_hash_before, "workspace_state_hash_before")
    _validate_digest(workspace_state_hash_after, "workspace_state_hash_after")
    if not isinstance(workspace_revision_before, int) or isinstance(workspace_revision_before, bool) or workspace_revision_before < 0:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "workspace_revision_before must be non-negative")
    _validate_paths(command_id, canonical_paths)
    expected_checkpoint = compute_multi_file_checkpoint_sha256(
        command_id=command_id,
        session_id=session_id,
        patch_sha256=patch_sha256,
        plan_sha256=plan_sha256,
        workspace_revision_before=workspace_revision_before,
        workspace_state_hash_before=workspace_state_hash_before,
        workspace_state_hash_after=workspace_state_hash_after,
        paths=canonical_paths,
    )
    if checkpoint_sha256 != expected_checkpoint:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "checkpoint_sha256 does not match canonical checkpoint")
    total_before = sum(len(item.before or b"") for item in canonical_paths)
    total_after = sum(len(item.after or b"") for item in canonical_paths)
    now = self._now_fn()
    with self._transaction():
        existing = get_multi_file_patch_bundle(self, command_id)
        if existing is not None:
            immutable = (
                existing.record.session_id,
                existing.record.patch_sha256,
                existing.record.plan_sha256,
                existing.record.checkpoint_sha256,
                existing.record.workspace_revision_before,
                existing.record.workspace_state_hash_before,
                existing.record.workspace_state_hash_after,
                existing.record.path_count,
                existing.record.total_before_bytes,
                existing.record.total_after_bytes,
                existing.paths,
            )
            supplied = (
                session_id,
                patch_sha256,
                plan_sha256,
                checkpoint_sha256,
                workspace_revision_before,
                workspace_state_hash_before,
                workspace_state_hash_after,
                len(canonical_paths),
                total_before,
                total_after,
                canonical_paths,
            )
            if immutable != supplied:
                raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Different immutable checkpoint already exists")
            return existing.record
        command = self.get_command(command_id)
        workspace = self.get_workspace(session_id)
        if command is None or command.session_id != session_id:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint command/session mismatch")
        if workspace is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint workspace is missing")
        if workspace.revision != workspace_revision_before or workspace.state_hash != workspace_state_hash_before:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint workspace before-state mismatch")
        self._connection.execute(
            """INSERT INTO multi_file_patch_checkpoints (
              command_id, session_id, patch_sha256, plan_sha256, checkpoint_sha256,
              state, workspace_revision_before, workspace_state_hash_before,
              workspace_revision_after, workspace_state_hash_after, path_count,
              total_before_bytes, total_after_bytes, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'planned', ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                command_id,
                session_id,
                patch_sha256,
                plan_sha256,
                checkpoint_sha256,
                workspace_revision_before,
                workspace_state_hash_before,
                workspace_state_hash_after,
                len(canonical_paths),
                total_before,
                total_after,
                now,
                now,
            ),
        )
        self._connection.executemany(
            """INSERT INTO multi_file_patch_checkpoint_paths (
              command_id, ordinal, path, before_exists, before_content, before_sha256,
              after_exists, after_content, after_sha256, roles_json, operation_indices_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    item.command_id,
                    item.ordinal,
                    item.path,
                    int(item.before_exists),
                    sqlite3.Binary(item.before) if item.before is not None else None,
                    item.before_sha256,
                    int(item.after_exists),
                    sqlite3.Binary(item.after) if item.after is not None else None,
                    item.after_sha256,
                    canonical_json(list(item.roles)),
                    canonical_json(list(item.operation_indices)),
                )
                for item in canonical_paths
            ],
        )
        self._append_event_in_transaction(
            session_id=session_id,
            command_id=command_id,
            event_type="multi_file_patch.checkpoint_recorded",
            payload={"checkpoint_sha256": checkpoint_sha256, "path_count": len(canonical_paths)},
            created_at=now,
        )
    record = get_multi_file_patch_checkpoint(self, command_id)
    assert record is not None
    return record


def _transition(
    self: Any,
    command_id: str,
    *,
    allowed: tuple[MultiFileCheckpointState, ...],
    target: MultiFileCheckpointState,
    event_type: str,
    last_error: str | None = None,
) -> MultiFileCheckpointRecord:
    self._ensure_open()
    now = self._now_fn()
    with self._transaction():
        current = get_multi_file_patch_checkpoint(self, command_id)
        if current is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint not found")
        if current.state is target:
            return current
        if current.state not in allowed:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"Cannot transition checkpoint from {current.state.value} to {target.value}",
            )
        updated = self._connection.execute(
            """UPDATE multi_file_patch_checkpoints
               SET state = ?, last_error = ?, updated_at = ?
               WHERE command_id = ? AND state = ?""",
            (target.value, last_error[:500] if last_error else None, now, command_id, current.state.value),
        )
        if updated.rowcount != 1:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint transition CAS failed")
        self._append_event_in_transaction(
            session_id=current.session_id,
            command_id=command_id,
            event_type=event_type,
            payload={"from_state": current.state.value, "to_state": target.value},
            created_at=now,
        )
    result = get_multi_file_patch_checkpoint(self, command_id)
    assert result is not None
    return result


def mark_multi_file_patch_applying(self: Any, command_id: str) -> MultiFileCheckpointRecord:
    return _transition(
        self,
        command_id,
        allowed=(MultiFileCheckpointState.PLANNED,),
        target=MultiFileCheckpointState.APPLYING,
        event_type="multi_file_patch.applying",
    )


def mark_multi_file_patch_applied(self: Any, command_id: str) -> MultiFileCheckpointRecord:
    return _transition(
        self,
        command_id,
        allowed=(MultiFileCheckpointState.APPLYING,),
        target=MultiFileCheckpointState.APPLIED,
        event_type="multi_file_patch.applied",
    )


def mark_multi_file_patch_rolling_back(self: Any, command_id: str) -> MultiFileCheckpointRecord:
    return _transition(
        self,
        command_id,
        allowed=(
            MultiFileCheckpointState.PLANNED,
            MultiFileCheckpointState.APPLYING,
            MultiFileCheckpointState.APPLIED,
        ),
        target=MultiFileCheckpointState.ROLLING_BACK,
        event_type="multi_file_patch.rolling_back",
    )


def mark_multi_file_patch_rolled_back(self: Any, command_id: str) -> MultiFileCheckpointRecord:
    return _transition(
        self,
        command_id,
        allowed=(MultiFileCheckpointState.ROLLING_BACK,),
        target=MultiFileCheckpointState.ROLLED_BACK,
        event_type="multi_file_patch.rolled_back",
    )


def block_multi_file_patch(self: Any, command_id: str, diagnostic: str) -> MultiFileCheckpointRecord:
    current = get_multi_file_patch_checkpoint(self, command_id)
    if current is None:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint not found")
    if current.state is MultiFileCheckpointState.BLOCKED:
        return current
    if current.state in {MultiFileCheckpointState.COMMITTED, MultiFileCheckpointState.ROLLED_BACK}:
        raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Terminal checkpoint cannot be blocked")
    return _transition(
        self,
        command_id,
        allowed=(current.state,),
        target=MultiFileCheckpointState.BLOCKED,
        event_type="multi_file_patch.blocked",
        last_error=str(diagnostic)[:500],
    )


def commit_multi_file_patch(self: Any, command_id: str) -> MultiFileCheckpointRecord:
    self._ensure_open()
    now = self._now_fn()
    with self._transaction():
        current = get_multi_file_patch_checkpoint(self, command_id)
        if current is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint not found")
        workspace = self.get_workspace(current.session_id)
        if workspace is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint workspace is missing")
        if current.state is MultiFileCheckpointState.COMMITTED:
            if (
                workspace.revision != current.workspace_revision_after
                or workspace.state_hash != current.workspace_state_hash_after
            ):
                raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Committed checkpoint/workspace mismatch")
            return current
        if current.state is not MultiFileCheckpointState.APPLIED:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Only applied checkpoint can commit")
        updated_workspace = self._connection.execute(
            """UPDATE workspaces SET revision = ?, state_hash = ?, updated_at = ?
               WHERE session_id = ? AND revision = ? AND state_hash = ?""",
            (
                current.workspace_revision_before + 1,
                current.workspace_state_hash_after,
                now,
                current.session_id,
                current.workspace_revision_before,
                current.workspace_state_hash_before,
            ),
        )
        if updated_workspace.rowcount != 1:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Workspace commit CAS failed")
        updated = self._connection.execute(
            """UPDATE multi_file_patch_checkpoints
               SET state = 'committed', workspace_revision_after = ?, updated_at = ?
               WHERE command_id = ? AND state = 'applied'""",
            (current.workspace_revision_before + 1, now, command_id),
        )
        if updated.rowcount != 1:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint commit CAS failed")
        self._append_event_in_transaction(
            session_id=current.session_id,
            command_id=command_id,
            event_type="multi_file_patch.committed",
            payload={
                "revision_before": current.workspace_revision_before,
                "revision_after": current.workspace_revision_before + 1,
                "state_hash_after": current.workspace_state_hash_after,
            },
            created_at=now,
        )
    result = get_multi_file_patch_checkpoint(self, command_id)
    assert result is not None
    return result


def list_incomplete_multi_file_patch_checkpoints(self: Any) -> tuple[MultiFileCheckpointRecord, ...]:
    self._ensure_open()
    rows = self._connection.execute(
        _RECORD_SELECT
        + " WHERE state IN ('planned','applying','applied','rolling_back') ORDER BY created_at, command_id"
    ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def install_journal_multi_file_patch_api(journal_cls: Type[object]) -> None:
    setattr(journal_cls, "get_multi_file_patch_checkpoint", get_multi_file_patch_checkpoint)
    setattr(journal_cls, "list_multi_file_patch_checkpoint_paths", list_multi_file_patch_checkpoint_paths)
    setattr(journal_cls, "get_multi_file_patch_bundle", get_multi_file_patch_bundle)
    setattr(journal_cls, "record_multi_file_patch_checkpoint", record_multi_file_patch_checkpoint)
    setattr(journal_cls, "mark_multi_file_patch_applying", mark_multi_file_patch_applying)
    setattr(journal_cls, "mark_multi_file_patch_applied", mark_multi_file_patch_applied)
    setattr(journal_cls, "mark_multi_file_patch_rolling_back", mark_multi_file_patch_rolling_back)
    setattr(journal_cls, "mark_multi_file_patch_rolled_back", mark_multi_file_patch_rolled_back)
    setattr(journal_cls, "block_multi_file_patch", block_multi_file_patch)
    setattr(journal_cls, "commit_multi_file_patch", commit_multi_file_patch)
    setattr(journal_cls, "list_incomplete_multi_file_patch_checkpoints", list_incomplete_multi_file_patch_checkpoints)
