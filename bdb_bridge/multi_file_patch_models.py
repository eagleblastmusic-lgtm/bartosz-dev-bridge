from __future__ import annotations

from dataclasses import dataclass

from .edit_operation_models import StructuralEditSpec


FILE_REPLACEMENT_SCHEMA = "bdb-file-replacement-v1"
MULTI_FILE_PATCH_SCHEMA = "bdb-multi-file-patch-v1"
MAX_BATCH_OPERATIONS = 100
MAX_BATCH_PATHS = 200
MAX_BATCH_CONTENT_BYTES = 8 * 1024 * 1024
MAX_BATCH_SNAPSHOT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FileReplacementSpec:
    schema: str
    kind: str
    path: str
    expected_sha256: str
    content: bytes
    content_sha256: str
    operation_sha256: str


BatchOperation = StructuralEditSpec | FileReplacementSpec


@dataclass(frozen=True)
class MultiFilePatchSpec:
    schema: str
    operations: tuple[BatchOperation, ...]
    operation_count: int
    supplied_content_bytes: int
    patch_sha256: str


@dataclass(frozen=True)
class PlannedPathState:
    path: str
    before_exists: bool
    before: bytes | None
    before_sha256: str | None
    after_exists: bool
    after: bytes | None
    after_sha256: str | None
    roles: tuple[str, ...]
    operation_indices: tuple[int, ...]


@dataclass(frozen=True)
class MultiFilePatchPlan:
    patch: MultiFilePatchSpec
    paths: tuple[PlannedPathState, ...]
    touched_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    total_before_bytes: int
    total_after_bytes: int
    plan_sha256: str
