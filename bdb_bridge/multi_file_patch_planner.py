from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .edit_operation_models import MAX_STRUCTURAL_CONTENT_BYTES, StructuralEditKind, StructuralEditSpec
from .edit_operation_parser import sha256_bytes
from .models import BridgeErrorCode
from .multi_file_patch_models import (
    MAX_BATCH_PATHS,
    MAX_BATCH_SNAPSHOT_BYTES,
    FileReplacementSpec,
    MultiFilePatchPlan,
    MultiFilePatchSpec,
    PlannedPathState,
)
from .multi_file_patch_parser import validate_multi_file_patch_spec
from .protocol import BridgeError
from .serializers import canonical_json
from .workspace_manager import WorkspaceManager


@dataclass
class _VirtualState:
    path: str
    resolved: Path
    before: bytes | None
    current: bytes | None
    roles: set[str] = field(default_factory=set)
    operation_indices: set[int] = field(default_factory=set)


class MultiFilePatchPlanner:
    """Build a complete, immutable before/after plan without mutating workspace."""

    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace

    def plan(self, patch: MultiFilePatchSpec) -> MultiFilePatchPlan:
        validate_multi_file_patch_spec(patch)
        states: dict[str, _VirtualState] = {}
        path_identities: dict[str, str] = {}
        for index, operation in enumerate(patch.operations):
            if isinstance(operation, FileReplacementSpec):
                state = self._state(states, path_identities, operation.path, "replace", index)
                self._require_present(state, operation.path)
                self._require_hash(state.current, operation.expected_sha256, operation.path)
                state.current = operation.content
                continue
            self._simulate_structural(states, path_identities, operation, index)

        paths = tuple(self._planned_state(state) for _, state in sorted(states.items()))
        changed_paths = tuple(
            item.path for item in paths if item.before_exists != item.after_exists or item.before != item.after
        )
        if not changed_paths:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file patch has no net changes")
        total_before = sum(len(item.before or b"") for item in paths)
        total_after = sum(len(item.after or b"") for item in paths)
        if total_before + total_after > MAX_BATCH_SNAPSHOT_BYTES:
            raise BridgeError(
                BridgeErrorCode.POLICY_DENIED,
                f"Multi-file before/after snapshot exceeds {MAX_BATCH_SNAPSHOT_BYTES} bytes",
            )
        candidate = MultiFilePatchPlan(
            patch=patch,
            paths=paths,
            touched_paths=tuple(item.path for item in paths),
            changed_paths=changed_paths,
            total_before_bytes=total_before,
            total_after_bytes=total_after,
            plan_sha256="",
        )
        return MultiFilePatchPlan(
            patch=candidate.patch,
            paths=candidate.paths,
            touched_paths=candidate.touched_paths,
            changed_paths=candidate.changed_paths,
            total_before_bytes=candidate.total_before_bytes,
            total_after_bytes=candidate.total_after_bytes,
            plan_sha256=self._plan_sha256(candidate),
        )

    def revalidate(self, plan: MultiFilePatchPlan) -> None:
        validate_multi_file_patch_spec(plan.patch)
        self._validate_plan_shape(plan)
        if plan.plan_sha256 != self._plan_sha256(plan):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file plan hash mismatch")
        expected = self.plan(plan.patch)
        if expected != plan:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Multi-file plan differs from the canonical workspace replan",
            )

    def _validate_plan_shape(self, plan: MultiFilePatchPlan) -> None:
        paths = tuple(item.path for item in plan.paths)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file plan paths are not canonical")
        identities = [_path_identity(path) for path in paths]
        if len(identities) != len(set(identities)):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file plan contains path aliases")
        if plan.touched_paths != paths:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file touched paths mismatch")
        changed: list[str] = []
        total_before = 0
        total_after = 0
        for item in plan.paths:
            if item.before_exists != (item.before is not None):
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file before existence mismatch")
            if item.after_exists != (item.after is not None):
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file after existence mismatch")
            expected_before_hash = sha256_bytes(item.before) if item.before is not None else None
            expected_after_hash = sha256_bytes(item.after) if item.after is not None else None
            if item.before_sha256 != expected_before_hash or item.after_sha256 != expected_after_hash:
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file plan bytes/hash mismatch")
            if len(item.before or b"") > MAX_STRUCTURAL_CONTENT_BYTES or len(item.after or b"") > MAX_STRUCTURAL_CONTENT_BYTES:
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file path bytes exceed per-file cap")
            if item.roles != tuple(sorted(set(item.roles))) or not item.roles:
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file path roles are not canonical")
            if item.operation_indices != tuple(sorted(set(item.operation_indices))) or not item.operation_indices:
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file operation indices are not canonical")
            if any(index < 0 or index >= plan.patch.operation_count for index in item.operation_indices):
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file operation index is out of range")
            if item.before_exists != item.after_exists or item.before != item.after:
                changed.append(item.path)
            total_before += len(item.before or b"")
            total_after += len(item.after or b"")
        if plan.changed_paths != tuple(changed) or not plan.changed_paths:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file changed paths mismatch")
        if plan.total_before_bytes != total_before or plan.total_after_bytes != total_after:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file plan byte totals mismatch")
        if total_before + total_after > MAX_BATCH_SNAPSHOT_BYTES:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file plan exceeds snapshot cap")

    def _simulate_structural(
        self,
        states: dict[str, _VirtualState],
        path_identities: dict[str, str],
        operation: StructuralEditSpec,
        index: int,
    ) -> None:
        if operation.kind is StructuralEditKind.CREATE_FILE:
            assert operation.destination_path is not None and operation.content is not None
            destination = self._state(
                states,
                path_identities,
                operation.destination_path,
                "create-destination",
                index,
                destination=True,
            )
            self._require_absent(destination, operation.destination_path)
            destination.current = operation.content
            return
        if operation.kind is StructuralEditKind.DELETE_FILE:
            assert operation.source_path is not None and operation.expected_source_sha256 is not None
            source = self._state(
                states, path_identities, operation.source_path, "delete-source", index
            )
            self._require_present(source, operation.source_path)
            self._require_hash(source.current, operation.expected_source_sha256, operation.source_path)
            source.current = None
            return
        assert (
            operation.source_path is not None
            and operation.destination_path is not None
            and operation.expected_source_sha256 is not None
        )
        source = self._state(
            states, path_identities, operation.source_path, "relocation-source", index
        )
        destination = self._state(
            states,
            path_identities,
            operation.destination_path,
            "relocation-destination",
            index,
            destination=True,
        )
        self._require_present(source, operation.source_path)
        self._require_hash(source.current, operation.expected_source_sha256, operation.source_path)
        self._require_absent(destination, operation.destination_path)
        destination.current = source.current
        source.current = None

    def _state(
        self,
        states: dict[str, _VirtualState],
        path_identities: dict[str, str],
        path: str,
        role: str,
        index: int,
        *,
        destination: bool = False,
    ) -> _VirtualState:
        state = states.get(path)
        if state is None:
            if len(states) >= MAX_BATCH_PATHS:
                raise BridgeError(
                    BridgeErrorCode.POLICY_DENIED,
                    f"Multi-file patch exceeds {MAX_BATCH_PATHS} unique paths",
                )
            identity = _path_identity(path)
            owner = path_identities.get(identity)
            if owner is not None and owner != path:
                raise BridgeError(
                    BridgeErrorCode.INVALID_PAYLOAD,
                    f"Multi-file paths alias after case/Unicode normalization: {owner} and {path}",
                )
            path_identities[identity] = path
            resolved = self.workspace.resolve_allowed_path(path)
            if destination and not resolved.parent.is_dir():
                raise BridgeError(
                    BridgeErrorCode.MISSING_FILE,
                    f"Destination parent directory does not exist: {resolved.parent.name}",
                )
            if resolved.exists() or resolved.is_symlink():
                if not resolved.is_file() or resolved.is_symlink():
                    raise BridgeError(
                        BridgeErrorCode.UNSAFE_PATH,
                        f"Multi-file path is not a regular file: {path}",
                    )
                before = self._read_bounded(resolved, path)
            else:
                before = None
            state = _VirtualState(path=path, resolved=resolved, before=before, current=before)
            states[path] = state
            if sum(len(item.before or b"") for item in states.values()) > MAX_BATCH_SNAPSHOT_BYTES:
                raise BridgeError(
                    BridgeErrorCode.POLICY_DENIED,
                    f"Multi-file before snapshot exceeds {MAX_BATCH_SNAPSHOT_BYTES} bytes",
                )
        elif destination and not state.resolved.parent.is_dir():
            raise BridgeError(
                BridgeErrorCode.MISSING_FILE,
                f"Destination parent directory does not exist: {state.resolved.parent.name}",
            )
        state.roles.add(role)
        state.operation_indices.add(index)
        return state

    @staticmethod
    def _require_present(state: _VirtualState, path: str) -> None:
        if state.current is None:
            raise BridgeError(BridgeErrorCode.MISSING_FILE, f"Batch source does not exist: {path}")

    @staticmethod
    def _require_absent(state: _VirtualState, path: str) -> None:
        if state.current is not None:
            raise BridgeError(BridgeErrorCode.STATE_MISMATCH, f"Batch destination already exists: {path}")

    @staticmethod
    def _require_hash(content: bytes | None, expected: str, path: str) -> None:
        if content is None or sha256_bytes(content) != expected:
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                f"Batch expected SHA-256 does not match virtual file state: {path}",
            )

    @staticmethod
    def _read_bounded(path: Path, relative: str) -> bytes:
        try:
            size = path.stat().st_size
            if size > MAX_STRUCTURAL_CONTENT_BYTES:
                raise BridgeError(
                    BridgeErrorCode.POLICY_DENIED,
                    f"Batch source exceeds {MAX_STRUCTURAL_CONTENT_BYTES} bytes: {relative}",
                )
            return path.read_bytes()
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Controlled batch source read failure: {type(exc).__name__}",
            ) from exc

    @staticmethod
    def _planned_state(state: _VirtualState) -> PlannedPathState:
        return PlannedPathState(
            path=state.path,
            before_exists=state.before is not None,
            before=state.before,
            before_sha256=sha256_bytes(state.before) if state.before is not None else None,
            after_exists=state.current is not None,
            after=state.current,
            after_sha256=sha256_bytes(state.current) if state.current is not None else None,
            roles=tuple(sorted(state.roles)),
            operation_indices=tuple(sorted(state.operation_indices)),
        )

    @staticmethod
    def _plan_payload(plan: MultiFilePatchPlan) -> dict[str, object]:
        return {
            "changed_paths": list(plan.changed_paths),
            "patch_sha256": plan.patch.patch_sha256,
            "paths": [
                {
                    "after_exists": item.after_exists,
                    "after_sha256": item.after_sha256,
                    "before_exists": item.before_exists,
                    "before_sha256": item.before_sha256,
                    "operation_indices": list(item.operation_indices),
                    "path": item.path,
                    "roles": list(item.roles),
                }
                for item in plan.paths
            ],
            "schema": "bdb-multi-file-plan-v1",
            "total_after_bytes": plan.total_after_bytes,
            "total_before_bytes": plan.total_before_bytes,
            "touched_paths": list(plan.touched_paths),
        }

    def _plan_sha256(self, plan: MultiFilePatchPlan) -> str:
        return sha256_bytes(canonical_json(self._plan_payload(plan)).encode("utf-8"))


def _path_identity(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()
