from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MultiFileCheckpointState(str, Enum):
    PLANNED = "planned"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    COMMITTED = "committed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class MultiFileCheckpointPath:
    command_id: str
    ordinal: int
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
class MultiFileCheckpointRecord:
    command_id: str
    session_id: str
    patch_sha256: str
    plan_sha256: str
    checkpoint_sha256: str
    state: MultiFileCheckpointState
    workspace_revision_before: int
    workspace_state_hash_before: str
    workspace_revision_after: int | None
    workspace_state_hash_after: str
    path_count: int
    total_before_bytes: int
    total_after_bytes: int
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MultiFileCheckpointBundle:
    record: MultiFileCheckpointRecord
    paths: tuple[MultiFileCheckpointPath, ...]


@dataclass(frozen=True)
class MultiFileRecoveryOutcome:
    command_id: str
    state: MultiFileCheckpointState
    action: str
    path_count: int
    workspace_revision: int
    workspace_state_hash: str
