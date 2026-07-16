from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


EDIT_OPERATION_SCHEMA = "bdb-edit-operation-v1"
MAX_STRUCTURAL_CONTENT_BYTES = 1 * 1024 * 1024


class StructuralEditKind(StrEnum):
    CREATE_FILE = "create_file"
    DELETE_FILE = "delete_file"
    RENAME_FILE = "rename_file"
    MOVE_FILE = "move_file"


@dataclass(frozen=True)
class StructuralEditSpec:
    schema: str
    kind: StructuralEditKind
    source_path: str | None
    destination_path: str | None
    content: bytes | None
    expected_source_sha256: str | None
    content_sha256: str | None
    operation_sha256: str


@dataclass(frozen=True)
class StructuralEditPlan:
    operation: StructuralEditSpec
    source_before: bytes | None
    destination_before: bytes | None
    source_before_sha256: str | None
    destination_before_sha256: str | None
    destination_after: bytes | None
    destination_after_sha256: str | None
    plan_sha256: str


@dataclass(frozen=True)
class StructuralEditOutcome:
    kind: StructuralEditKind
    source_path: str | None
    destination_path: str | None
    source_exists_after: bool
    destination_exists_after: bool
    destination_sha256_after: str | None
    changed_paths: tuple[str, ...]
    plan_sha256: str
    outcome_sha256: str
