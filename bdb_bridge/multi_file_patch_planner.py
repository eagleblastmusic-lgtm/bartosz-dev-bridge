from __future__ import annotations

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
        for index, operation in enumerate(patch.operations):
            if isinstance(operation, FileReplacementSpec):
                state = self._state(states, operation.path, "replace", index)
                self._require_present(state, operation.path)
                self._require_hash(state.current, operation.expected_sha256, operation.path)
                state.current = operation.content
                continue
            self._simulate_structural(states, operation, index)

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
        if plan.plan_sha256 != self._plan_sha256(plan):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file plan hash mismatch")
        for item in plan.paths:
            resolved = self.workspace.resolve_allowed_path(item.path)
            if item.before_exists:
                if not resolved.is_file():
                    raise BridgeError(
                        BridgeErrorCode.STATE_MISMATCH,
                        f"Planned source disappeared or changed type: {item.path}",
                    )
                actual = self._read_bounded(resolved, item.path)
                if actual != item.before:
                    raise BridgeError(
                        BridgeErrorCode.STATE_MISMATCH,
                        f"Planned source bytes changed: {item.path}",
                    )
            elif resolved.exists() or resolved.is_symlink():
                raise BridgeError(
                    BridgeErrorCode.STATE_MISMATCH,
                    f"Planned absent path appeared: {item.path}",
                )

    def _simulate_structural(
        self,
        states: dict[str, _VirtualState],
        operation: StructuralEditSpec,
        index: int,
    ) -> None:
        if operation.kind is StructuralEditKind.CREATE_FILE:
            assert operation.destination_path is not None and operation.content is not None
            destination = self._state(
                states, operation.destination_path, "create-destination", index, destination=True
            )
            self._require_absent(destination, operation.destination_path)
            destination.current = operation.content
            return
        if operation.kind is StructuralEditKind.DELETE_FILE:
            assert operation.source_path is not None and operation.expected_source_sha256 is not None
            source = self._state(states, operation.source_path, "delete-source", index)
            self._require_present(source, operation.source_path)
            self._require_hash(source.current, operation.expected_source_sha256, operation.source_path)
            source.current = None
            return
        assert (
            operation.source_path is not None
            and operation.destination_path is not None
            and operation.expected_source_sha256 is not None
        )
        source = self._state(states, operation.source_path, "relocation-source", index)
        destination = self._state(
            states, operation.destination_path, "relocation-destination", index, destination=True
        )
        self._require_present(source, operation.source_path)
        self._require_hash(source.current, operation.expected_source_sha256, operation.source_path)
        self._require_absent(destination, operation.destination_path)
        destination.current = source.current
        source.current = None

    def _state(
        self,
        states: dict[str, _VirtualState],
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
