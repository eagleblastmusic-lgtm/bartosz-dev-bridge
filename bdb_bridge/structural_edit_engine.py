from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from .edit_operation_models import (
    MAX_STRUCTURAL_CONTENT_BYTES,
    StructuralEditKind,
    StructuralEditOutcome,
    StructuralEditPlan,
    StructuralEditSpec,
)
from .edit_operation_parser import sha256_bytes, validate_structural_edit_spec
from .models import BridgeErrorCode
from .protocol import BridgeError
from .serializers import canonical_json
from .workspace_manager import WorkspaceManager


class StructuralEditEngine:
    """Plan and apply one bounded structural file operation.

    The caller owns process-level serialization. This engine intentionally does
    not register legacy GHB0 operation plans/effects; durable checkpoints and
    crash recovery are introduced in GHB2-C.
    """

    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace

    def plan(self, operation: StructuralEditSpec) -> StructuralEditPlan:
        validate_structural_edit_spec(operation)
        source_before: bytes | None = None
        destination_before: bytes | None = None
        destination_after: bytes | None = None

        if operation.kind is StructuralEditKind.CREATE_FILE:
            assert operation.destination_path is not None and operation.content is not None
            destination = self._destination(operation.destination_path)
            self._require_absent(destination, operation.destination_path)
            destination_after = operation.content
        elif operation.kind is StructuralEditKind.DELETE_FILE:
            assert operation.source_path is not None
            source = self._source(operation.source_path)
            source_before = self._read_bounded(source, operation.source_path)
            self._require_expected_source(operation, source_before)
        else:
            assert operation.source_path is not None and operation.destination_path is not None
            source = self._source(operation.source_path)
            destination = self._destination(operation.destination_path)
            source_before = self._read_bounded(source, operation.source_path)
            self._require_expected_source(operation, source_before)
            self._require_absent(destination, operation.destination_path)
            destination_after = source_before

        source_hash = sha256_bytes(source_before) if source_before is not None else None
        destination_hash = (
            sha256_bytes(destination_before) if destination_before is not None else None
        )
        after_hash = sha256_bytes(destination_after) if destination_after is not None else None
        candidate = StructuralEditPlan(
            operation=operation,
            source_before=source_before,
            destination_before=destination_before,
            source_before_sha256=source_hash,
            destination_before_sha256=destination_hash,
            destination_after=destination_after,
            destination_after_sha256=after_hash,
            plan_sha256="",
        )
        return replace(candidate, plan_sha256=self._plan_sha256(candidate))

    def apply(self, plan: StructuralEditPlan) -> StructuralEditOutcome:
        self._validate_plan(plan)
        operation = plan.operation
        if operation.kind is StructuralEditKind.CREATE_FILE:
            self._apply_create(plan)
        elif operation.kind is StructuralEditKind.DELETE_FILE:
            self._apply_delete(plan)
        else:
            self._apply_relocate(plan)
        return self._outcome(plan)

    def _apply_create(self, plan: StructuralEditPlan) -> None:
        operation = plan.operation
        assert operation.destination_path is not None and plan.destination_after is not None
        destination = self._destination(operation.destination_path)
        self._require_absent(destination, operation.destination_path)
        temp = self._temp_path(destination, plan)
        if temp.exists() or temp.is_symlink():
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                "Expected structural edit temp path already exists",
            )
        try:
            with temp.open("xb") as stream:
                stream.write(plan.destination_after)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                "Expected structural edit temp path appeared during create",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Controlled structural temp write failure: {type(exc).__name__}",
            ) from exc
        try:
            if temp.read_bytes() != plan.destination_after:
                raise OSError("temp reread mismatch")
            self._require_absent(destination, operation.destination_path)
            os.link(temp, destination)
            temp.unlink()
            self._fsync_parent(destination.parent)
        except FileExistsError as exc:
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                "Structural edit destination appeared during create",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Controlled structural create promotion failure: {type(exc).__name__}",
            ) from exc
        self._verify_regular_exact(destination, plan.destination_after, operation.destination_path)

    def _apply_delete(self, plan: StructuralEditPlan) -> None:
        operation = plan.operation
        assert operation.source_path is not None and plan.source_before is not None
        source = self._source(operation.source_path)
        actual = self._read_bounded(source, operation.source_path)
        if actual != plan.source_before:
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                "Structural edit source changed after planning",
            )
        try:
            source.unlink()
            self._fsync_parent(source.parent)
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Controlled structural delete failure: {type(exc).__name__}",
            ) from exc
        if source.exists() or source.is_symlink():
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                "Structural delete source still exists after unlink",
            )

    def _apply_relocate(self, plan: StructuralEditPlan) -> None:
        operation = plan.operation
        assert (
            operation.source_path is not None
            and operation.destination_path is not None
            and plan.source_before is not None
            and plan.destination_after is not None
        )
        source = self._source(operation.source_path)
        destination = self._destination(operation.destination_path)
        actual = self._read_bounded(source, operation.source_path)
        if actual != plan.source_before:
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                "Structural edit source changed after planning",
            )
        self._require_absent(destination, operation.destination_path)
        try:
            os.link(source, destination)
            source.unlink()
            self._fsync_parent(source.parent)
            if destination.parent != source.parent:
                self._fsync_parent(destination.parent)
        except FileExistsError as exc:
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                "Structural edit destination appeared during relocation",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Controlled structural relocation failure: {type(exc).__name__}",
            ) from exc
        if source.exists() or source.is_symlink():
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                "Structural relocation source still exists after promotion",
            )
        self._verify_regular_exact(
            destination, plan.destination_after, operation.destination_path
        )

    def _source(self, relative: str) -> Path:
        path = self.workspace.resolve_allowed_path(relative)
        if not path.is_file():
            raise BridgeError(BridgeErrorCode.MISSING_FILE, f"Source file does not exist: {relative}")
        return path

    def _destination(self, relative: str) -> Path:
        path = self.workspace.resolve_allowed_path(relative)
        parent = path.parent
        if not parent.is_dir():
            raise BridgeError(
                BridgeErrorCode.MISSING_FILE,
                f"Destination parent directory does not exist: {parent.name}",
            )
        return path

    @staticmethod
    def _require_absent(path: Path, relative: str) -> None:
        if path.exists() or path.is_symlink():
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                f"Destination must not exist: {relative}",
            )

    @staticmethod
    def _read_bounded(path: Path, relative: str) -> bytes:
        try:
            size = path.stat().st_size
            if size > MAX_STRUCTURAL_CONTENT_BYTES:
                raise BridgeError(
                    BridgeErrorCode.POLICY_DENIED,
                    f"Structural source exceeds {MAX_STRUCTURAL_CONTENT_BYTES} bytes: {relative}",
                )
            return path.read_bytes()
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Controlled structural source read failure: {type(exc).__name__}",
            ) from exc

    @staticmethod
    def _require_expected_source(operation: StructuralEditSpec, actual: bytes) -> None:
        if operation.expected_source_sha256 != sha256_bytes(actual):
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                "expected source SHA-256 does not match exact file bytes",
            )

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        WorkspaceManager._fsync_parent(path)

    @staticmethod
    def _verify_regular_exact(path: Path, expected: bytes, relative: str) -> None:
        if not path.is_file() or path.is_symlink():
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Structural destination is not a regular file: {relative}",
            )
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                f"Controlled structural destination read failure: {type(exc).__name__}",
            ) from exc
        if actual != expected:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                "Structural destination bytes differ after apply",
            )

    def _temp_path(self, destination: Path, plan: StructuralEditPlan) -> Path:
        suffix = plan.plan_sha256.removeprefix("sha256:")[:16]
        if len(suffix) != 16 or any(ch not in "0123456789abcdef" for ch in suffix):
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "Invalid structural edit plan hash",
            )
        temp = destination.parent / f".bdb_edit_{destination.name}_{suffix}"
        try:
            temp.resolve(strict=False).relative_to(self.workspace.path.resolve(strict=False))
        except ValueError as exc:
            raise BridgeError(BridgeErrorCode.UNSAFE_PATH, "Structural temp path escaped workspace") from exc
        return temp

    @staticmethod
    def _plan_payload(plan: StructuralEditPlan) -> dict[str, object]:
        return {
            "destination_after_sha256": plan.destination_after_sha256,
            "destination_before_sha256": plan.destination_before_sha256,
            "operation_sha256": plan.operation.operation_sha256,
            "schema": "bdb-structural-edit-plan-v1",
            "source_before_sha256": plan.source_before_sha256,
        }

    def _plan_sha256(self, plan: StructuralEditPlan) -> str:
        return sha256_bytes(canonical_json(self._plan_payload(plan)).encode("utf-8"))

    def _validate_plan(self, plan: StructuralEditPlan) -> None:
        validate_structural_edit_spec(plan.operation)
        if plan.plan_sha256 != self._plan_sha256(plan):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Structural edit plan hash mismatch")
        if (
            (plan.source_before is None and plan.source_before_sha256 is not None)
            or (
                plan.source_before is not None
                and sha256_bytes(plan.source_before) != plan.source_before_sha256
            )
        ):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Structural source plan bytes mismatch")
        if (
            (plan.destination_after is None and plan.destination_after_sha256 is not None)
            or (
                plan.destination_after is not None
                and sha256_bytes(plan.destination_after) != plan.destination_after_sha256
            )
        ):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Structural destination plan bytes mismatch")
        if plan.destination_before is not None or plan.destination_before_sha256 is not None:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "GHB2-A structural destination must be absent before apply",
            )
        operation = plan.operation
        if operation.kind is StructuralEditKind.CREATE_FILE:
            if (
                plan.source_before is not None
                or plan.source_before_sha256 is not None
                or plan.destination_after != operation.content
            ):
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid create_file plan shape")
        elif operation.kind is StructuralEditKind.DELETE_FILE:
            if (
                plan.source_before is None
                or plan.source_before_sha256 != operation.expected_source_sha256
                or plan.destination_after is not None
                or plan.destination_after_sha256 is not None
            ):
                raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid delete_file plan shape")
        elif (
            plan.source_before is None
            or plan.source_before_sha256 != operation.expected_source_sha256
            or plan.destination_after != plan.source_before
        ):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid relocation plan shape")

    def _outcome(self, plan: StructuralEditPlan) -> StructuralEditOutcome:
        operation = plan.operation
        source_exists = (
            self.workspace.resolve_allowed_path(operation.source_path).exists()
            if operation.source_path is not None
            else False
        )
        destination_exists = (
            self.workspace.resolve_allowed_path(operation.destination_path).exists()
            if operation.destination_path is not None
            else False
        )
        destination_sha = None
        if operation.destination_path is not None and destination_exists:
            destination_path = self.workspace.resolve_allowed_path(operation.destination_path)
            destination_sha = sha256_bytes(
                self._read_bounded(destination_path, operation.destination_path)
            )
        changed = tuple(
            sorted(
                path
                for path in {operation.source_path, operation.destination_path}
                if path is not None
            )
        )
        payload: dict[str, object] = {
            "changed_paths": list(changed),
            "destination_exists_after": destination_exists,
            "destination_path": operation.destination_path,
            "destination_sha256_after": destination_sha,
            "kind": operation.kind.value,
            "plan_sha256": plan.plan_sha256,
            "schema": "bdb-structural-edit-outcome-v1",
            "source_exists_after": source_exists,
            "source_path": operation.source_path,
        }
        outcome_sha = sha256_bytes(canonical_json(payload).encode("utf-8"))
        return StructuralEditOutcome(
            kind=operation.kind,
            source_path=operation.source_path,
            destination_path=operation.destination_path,
            source_exists_after=source_exists,
            destination_exists_after=destination_exists,
            destination_sha256_after=destination_sha,
            changed_paths=changed,
            plan_sha256=plan.plan_sha256,
            outcome_sha256=outcome_sha,
        )
