from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Type

from . import multi_file_patch_journal as _journal
from .edit_operation_models import MAX_STRUCTURAL_CONTENT_BYTES
from .edit_operation_parser import sha256_bytes
from .instance_lock import InstanceLock
from .migrations import map_sqlite_error
from .models import BridgeErrorCode
from .multi_file_patch_journal import compute_multi_file_checkpoint_sha256
from .multi_file_patch_models import MAX_BATCH_PATHS, MAX_BATCH_SNAPSHOT_BYTES, MultiFilePatchPlan
from .multi_file_patch_recovery_models import (
    MultiFileCheckpointBundle,
    MultiFileCheckpointPath,
    MultiFileCheckpointRecord,
    MultiFileCheckpointState,
)
from .protocol import BridgeError, validate_repo_relative_path, validate_session_id
from .workspace_types import WorkspaceDisposition, WorkspaceLifecycleState


def _code(corrupt: bool) -> BridgeErrorCode:
    return BridgeErrorCode.JOURNAL_CORRUPT if corrupt else BridgeErrorCode.INVALID_PAYLOAD


def _canonical_digest(value: object, field: str, *, corrupt: bool) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise BridgeError(_code(corrupt), f"Invalid canonical digest in {field}")
    return value


def _require_int(value: object, field: str, *, minimum: int = 0, corrupt: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BridgeError(_code(corrupt), f"Invalid integer in {field}")
    return value


def _validate_checkpoint_path(
    command_id: str,
    item: MultiFileCheckpointPath,
    *,
    corrupt: bool,
) -> int:
    code = _code(corrupt)
    try:
        normalized = validate_repo_relative_path(item.path)
    except BridgeError as exc:
        raise BridgeError(code, "Checkpoint contains an invalid repository path") from exc
    if normalized != item.path:
        raise BridgeError(code, "Checkpoint path is not canonical")
    if item.command_id != command_id:
        raise BridgeError(code, "Checkpoint path command mismatch")
    if item.before_exists != (item.before is not None):
        raise BridgeError(code, "Checkpoint before existence mismatch")
    if item.after_exists != (item.after is not None):
        raise BridgeError(code, "Checkpoint after existence mismatch")
    before_size = len(item.before or b"")
    after_size = len(item.after or b"")
    if before_size > MAX_STRUCTURAL_CONTENT_BYTES or after_size > MAX_STRUCTURAL_CONTENT_BYTES:
        raise BridgeError(code, "Checkpoint path exceeds the per-file snapshot cap")
    before_hash = sha256_bytes(item.before) if item.before is not None else None
    after_hash = sha256_bytes(item.after) if item.after is not None else None
    if item.before_sha256 != before_hash or item.after_sha256 != after_hash:
        raise BridgeError(code, "Checkpoint path bytes/hash mismatch")
    if item.before_exists == item.after_exists and item.before == item.after:
        raise BridgeError(code, "Checkpoint path has no net change")
    if item.roles != tuple(sorted(set(item.roles))) or not item.roles:
        raise BridgeError(code, "Checkpoint path roles are not canonical")
    if item.operation_indices != tuple(sorted(set(item.operation_indices))) or not item.operation_indices:
        raise BridgeError(code, "Checkpoint operation indices are not canonical")
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in item.operation_indices):
        raise BridgeError(code, "Checkpoint operation index is invalid")
    return before_size + after_size


def _validate_checkpoint_paths(
    command_id: str,
    paths: tuple[MultiFileCheckpointPath, ...],
    *,
    corrupt: bool = False,
) -> None:
    code = _code(corrupt)
    if not paths or len(paths) > MAX_BATCH_PATHS:
        raise BridgeError(code, f"Checkpoint path count must be in 1..{MAX_BATCH_PATHS}")
    if tuple(item.ordinal for item in paths) != tuple(range(len(paths))):
        raise BridgeError(code, "Checkpoint path ordinals are not contiguous")
    if tuple(item.path for item in paths) != tuple(sorted(item.path for item in paths)):
        raise BridgeError(code, "Checkpoint paths are not sorted")
    if len({item.path for item in paths}) != len(paths):
        raise BridgeError(code, "Checkpoint paths are not unique")
    total = sum(_validate_checkpoint_path(command_id, item, corrupt=corrupt) for item in paths)
    if total > MAX_BATCH_SNAPSHOT_BYTES:
        raise BridgeError(code, "Checkpoint before/after snapshot exceeds the batch cap")


def _row_to_checkpoint_record(row: sqlite3.Row | tuple[Any, ...]) -> MultiFileCheckpointRecord:
    try:
        state = MultiFileCheckpointState(str(row[5]))
        revision_before = _require_int(row[6], "workspace_revision_before", corrupt=True)
        revision_after = (
            None
            if row[8] is None
            else _require_int(row[8], "workspace_revision_after", corrupt=True)
        )
        record = MultiFileCheckpointRecord(
            command_id=str(row[0]),
            session_id=str(row[1]),
            patch_sha256=_canonical_digest(row[2], "patch_sha256", corrupt=True),
            plan_sha256=_canonical_digest(row[3], "plan_sha256", corrupt=True),
            checkpoint_sha256=_canonical_digest(row[4], "checkpoint_sha256", corrupt=True),
            state=state,
            workspace_revision_before=revision_before,
            workspace_state_hash_before=_canonical_digest(
                row[7], "workspace_state_hash_before", corrupt=True
            ),
            workspace_revision_after=revision_after,
            workspace_state_hash_after=_canonical_digest(
                row[9], "workspace_state_hash_after", corrupt=True
            ),
            path_count=_require_int(row[10], "path_count", minimum=1, corrupt=True),
            total_before_bytes=_require_int(row[11], "total_before_bytes", corrupt=True),
            total_after_bytes=_require_int(row[12], "total_after_bytes", corrupt=True),
            last_error=None if row[13] is None else str(row[13]),
            created_at=str(row[14]),
            updated_at=str(row[15]),
        )
        validate_session_id(record.session_id)
        if not record.command_id or record.path_count > MAX_BATCH_PATHS:
            raise ValueError("invalid checkpoint identity")
        if record.total_before_bytes + record.total_after_bytes > MAX_BATCH_SNAPSHOT_BYTES:
            raise ValueError("oversized checkpoint snapshot")
        if record.last_error is not None and len(record.last_error) > 500:
            raise ValueError("oversized checkpoint diagnostic")
        if state is MultiFileCheckpointState.COMMITTED:
            if revision_after != revision_before + 1:
                raise ValueError("invalid committed revision")
        elif revision_after is not None:
            raise ValueError("non-committed checkpoint has revision_after")
        return record
    except BridgeError as exc:
        if exc.code == BridgeErrorCode.JOURNAL_CORRUPT.value:
            raise
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Invalid multi-file checkpoint header",
        ) from exc
    except (IndexError, TypeError, ValueError) as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Invalid multi-file checkpoint header",
        ) from exc


def _row_to_checkpoint_path(row: sqlite3.Row | tuple[Any, ...]) -> MultiFileCheckpointPath:
    try:
        before_exists_raw = _require_int(row[3], "before_exists", corrupt=True)
        after_exists_raw = _require_int(row[6], "after_exists", corrupt=True)
        if before_exists_raw not in (0, 1) or after_exists_raw not in (0, 1):
            raise ValueError("invalid existence flag")
        roles_raw = json.loads(str(row[9]))
        indices_raw = json.loads(str(row[10]))
        if not isinstance(roles_raw, list) or not all(isinstance(value, str) for value in roles_raw):
            raise ValueError("invalid roles JSON")
        if not isinstance(indices_raw, list) or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in indices_raw
        ):
            raise ValueError("invalid operation indices JSON")
        item = MultiFileCheckpointPath(
            command_id=str(row[0]),
            ordinal=_require_int(row[1], "ordinal", corrupt=True),
            path=str(row[2]),
            before_exists=bool(before_exists_raw),
            before=bytes(row[4]) if row[4] is not None else None,
            before_sha256=(
                None
                if row[5] is None
                else _canonical_digest(row[5], "before_sha256", corrupt=True)
            ),
            after_exists=bool(after_exists_raw),
            after=bytes(row[7]) if row[7] is not None else None,
            after_sha256=(
                None
                if row[8] is None
                else _canonical_digest(row[8], "after_sha256", corrupt=True)
            ),
            roles=tuple(roles_raw),
            operation_indices=tuple(indices_raw),
        )
        _validate_checkpoint_path(item.command_id, item, corrupt=True)
        return item
    except BridgeError as exc:
        if exc.code == BridgeErrorCode.JOURNAL_CORRUPT.value:
            raise
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Invalid multi-file checkpoint path row",
        ) from exc
    except (IndexError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Invalid multi-file checkpoint path row",
        ) from exc


def _get_checkpoint(self: Any, command_id: str) -> MultiFileCheckpointRecord | None:
    self._ensure_open()
    try:
        row = self._connection.execute(
            _journal._RECORD_SELECT + " WHERE command_id = ?",
            (command_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="multi-file checkpoint read") from exc
    return None if row is None else _row_to_checkpoint_record(row)


def _list_checkpoint_paths(
    self: Any,
    command_id: str,
) -> tuple[MultiFileCheckpointPath, ...]:
    self._ensure_open()
    try:
        rows = self._connection.execute(
            _journal._PATH_SELECT + " WHERE command_id = ? ORDER BY ordinal ASC",
            (command_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="multi-file checkpoint path read") from exc
    paths = tuple(_row_to_checkpoint_path(row) for row in rows)
    if paths:
        _validate_checkpoint_paths(command_id, paths, corrupt=True)
    return paths


def _get_checkpoint_bundle(self: Any, command_id: str) -> MultiFileCheckpointBundle | None:
    record = _get_checkpoint(self, command_id)
    if record is None:
        return None
    paths = _list_checkpoint_paths(self, command_id)
    if len(paths) != record.path_count:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Checkpoint path count differs from header",
        )
    total_before = sum(len(item.before or b"") for item in paths)
    total_after = sum(len(item.after or b"") for item in paths)
    if (total_before, total_after) != (
        record.total_before_bytes,
        record.total_after_bytes,
    ):
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Checkpoint byte totals differ from header",
        )
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


def _list_incomplete_checkpoints(
    self: Any,
    session_id: str | None = None,
) -> tuple[MultiFileCheckpointRecord, ...]:
    self._ensure_open()
    params: tuple[object, ...] = ()
    where = "state IN ('planned','applying','applied','rolling_back')"
    if session_id is not None:
        validate_session_id(session_id)
        where += " AND session_id = ?"
        params = (session_id,)
    try:
        rows = self._connection.execute(
            _journal._RECORD_SELECT
            + f" WHERE {where} ORDER BY created_at, command_id",
            params,
        ).fetchall()
    except sqlite3.Error as exc:
        raise map_sqlite_error(
            exc,
            context="incomplete multi-file checkpoint read",
        ) from exc
    return tuple(_row_to_checkpoint_record(row) for row in rows)


def install_journal_multi_file_patch_hardening(journal_cls: Type[object]) -> None:
    """Install bounded, corruption-safe checkpoint read and validation surfaces."""

    if getattr(journal_cls, "_ghb2c_hardening_installed", False):
        return
    _journal._validate_paths = _validate_checkpoint_paths
    _journal._row_to_record = _row_to_checkpoint_record
    _journal._row_to_path = _row_to_checkpoint_path
    _journal.get_multi_file_patch_checkpoint = _get_checkpoint
    _journal.list_multi_file_patch_checkpoint_paths = _list_checkpoint_paths
    _journal.get_multi_file_patch_bundle = _get_checkpoint_bundle
    _journal.list_incomplete_multi_file_patch_checkpoints = _list_incomplete_checkpoints
    setattr(journal_cls, "get_multi_file_patch_checkpoint", _get_checkpoint)
    setattr(journal_cls, "list_multi_file_patch_checkpoint_paths", _list_checkpoint_paths)
    setattr(journal_cls, "get_multi_file_patch_bundle", _get_checkpoint_bundle)
    setattr(
        journal_cls,
        "list_incomplete_multi_file_patch_checkpoints",
        _list_incomplete_checkpoints,
    )
    setattr(journal_cls, "_ghb2c_hardening_installed", True)


def _short_temp_path(
    self: Any,
    bundle: MultiFileCheckpointBundle,
    item: MultiFileCheckpointPath,
    mode: str,
) -> Path:
    target = self.workspace.resolve_allowed_path(item.path)
    checkpoint = bundle.record.checkpoint_sha256[7:23]
    path_digest = hashlib.sha256(item.path.encode("utf-8")).hexdigest()[:16]
    return target.parent / f".bdb_batch_{checkpoint}_{path_digest}_{item.ordinal}_{mode}"


def install_multi_file_patch_executor_hardening(executor_cls: Type[object]) -> None:
    """Require exclusive Bridge ownership and session-scoped recovery."""

    if getattr(executor_cls, "_ghb2c_hardening_installed", False):
        return

    original_init = executor_cls.__init__
    original_checkpoint = executor_cls.checkpoint
    original_apply = executor_cls.apply
    original_rollback = executor_cls.rollback
    original_commit = executor_cls.commit
    original_recover = executor_cls.recover
    original_require_bundle = executor_cls._require_bundle

    def hardened_init(
        self: Any,
        workspace: Any,
        journal: Any,
        *,
        instance_lock: InstanceLock | None = None,
    ) -> None:
        original_init(self, workspace, journal)
        self.instance_lock = instance_lock

    def require_lock(self: Any) -> None:
        runtime_dir = getattr(self.workspace.config, "runtime_dir", None)
        if runtime_dir is None:
            raise BridgeError(
                BridgeErrorCode.INVALID_CONFIG,
                "GHB2-C requires a configured runtime_dir",
            )
        expected = (Path(runtime_dir) / "bridge.instance.lock").expanduser().resolve()
        lock = self.instance_lock
        if (
            not isinstance(lock, InstanceLock)
            or lock.path != expected
            or not lock.is_acquired
        ):
            raise BridgeError(
                BridgeErrorCode.INSTANCE_LOCK_FAILED,
                "GHB2-C requires ownership of the canonical bridge.instance.lock",
            )

    def require_lifecycle(self: Any) -> None:
        workspace = self.journal.get_workspace(self.workspace.session_id)
        lifecycle = self.journal.get_workspace_lifecycle(self.workspace.session_id)
        if workspace is None or lifecycle is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Workspace and preserved lifecycle records are required for GHB2-C",
            )
        durable_identity = (
            lifecycle.workspace_path,
            lifecycle.base_sha,
            lifecycle.expected_revision,
            lifecycle.expected_state_hash,
        )
        active_identity = (
            str(self.workspace.path),
            self.workspace.base_sha,
            workspace.revision,
            workspace.state_hash,
        )
        if durable_identity != active_identity:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                "Workspace lifecycle identity differs from the active workspace",
            )
        if (
            lifecycle.disposition is not WorkspaceDisposition.PRESERVE
            or lifecycle.state is not WorkspaceLifecycleState.PRESERVED
        ):
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                "Workspace lifecycle must remain preserved during GHB2-C",
            )

    def require_bundle(self: Any, command_id: str) -> MultiFileCheckpointBundle:
        bundle = original_require_bundle(self, command_id)
        if bundle.record.session_id != self.workspace.session_id:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Checkpoint belongs to a different workspace session",
            )
        return bundle

    def preflight_temp_ownership(
        self: Any,
        *,
        command_id: str,
        session_id: str,
        plan: MultiFilePatchPlan,
    ) -> None:
        if self.journal.get_multi_file_patch_checkpoint(command_id) is not None:
            return
        self.planner.revalidate(plan)
        workspace_record = self.journal.get_workspace(session_id)
        if workspace_record is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Workspace is not registered",
            )
        path_by_name = {item.path: item for item in plan.paths}
        changed = [path_by_name[path] for path in plan.changed_paths]
        paths = tuple(
            MultiFileCheckpointPath(
                command_id=command_id,
                ordinal=ordinal,
                path=item.path,
                before_exists=item.before_exists,
                before=item.before,
                before_sha256=item.before_sha256,
                after_exists=item.after_exists,
                after=item.after,
                after_sha256=item.after_sha256,
                roles=item.roles,
                operation_indices=item.operation_indices,
            )
            for ordinal, item in enumerate(changed)
        )
        after_hash = self._predicted_state_hash(paths, after=True)
        checkpoint_sha256 = compute_multi_file_checkpoint_sha256(
            command_id=command_id,
            session_id=session_id,
            patch_sha256=plan.patch.patch_sha256,
            plan_sha256=plan.plan_sha256,
            workspace_revision_before=workspace_record.revision,
            workspace_state_hash_before=workspace_record.state_hash,
            workspace_state_hash_after=after_hash,
            paths=paths,
        )
        pseudo = SimpleNamespace(
            record=SimpleNamespace(checkpoint_sha256=checkpoint_sha256),
            paths=paths,
        )
        for item in paths:
            for mode in ("apply", "rollback"):
                temp = self._temp_path(pseudo, item, mode)
                if temp.exists() or temp.is_symlink():
                    raise BridgeError(
                        BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                        f"Pre-existing internal temp collision: {temp.name}",
                    )

    def checkpoint(self: Any, **kwargs: Any) -> MultiFileCheckpointBundle:
        require_lock(self)
        require_lifecycle(self)
        preflight_temp_ownership(self, **kwargs)
        return original_checkpoint(self, **kwargs)

    def apply(self: Any, command_id: str, **kwargs: Any):
        require_lock(self)
        require_lifecycle(self)
        return original_apply(self, command_id, **kwargs)

    def rollback(self: Any, command_id: str, **kwargs: Any):
        require_lock(self)
        require_lifecycle(self)
        return original_rollback(self, command_id, **kwargs)

    def commit(self: Any, command_id: str):
        require_lock(self)
        require_lifecycle(self)
        return original_commit(self, command_id)

    def recover(self: Any, command_id: str):
        require_lock(self)
        require_lifecycle(self)
        return original_recover(self, command_id)

    def recover_all(self: Any):
        require_lock(self)
        require_lifecycle(self)
        return tuple(
            self.recover(record.command_id)
            for record in self.journal.list_incomplete_multi_file_patch_checkpoints(
                self.workspace.session_id
            )
        )

    executor_cls.__init__ = hardened_init
    executor_cls._require_instance_lock = require_lock
    executor_cls._require_preserved_lifecycle = require_lifecycle
    executor_cls._require_bundle = require_bundle
    executor_cls._temp_path = _short_temp_path
    executor_cls.checkpoint = checkpoint
    executor_cls.apply = apply
    executor_cls.rollback = rollback
    executor_cls.commit = commit
    executor_cls.recover = recover
    executor_cls.recover_all = recover_all
    setattr(executor_cls, "_ghb2c_hardening_installed", True)
