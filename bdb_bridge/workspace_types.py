from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkspaceDisposition(StrEnum):
    PRESERVE = "preserve"
    CLEANUP = "cleanup"


class WorkspaceLifecycleState(StrEnum):
    PRESERVED = "preserved"
    CLEANUP_REQUESTED = "cleanup_requested"
    REMOVING = "removing"
    REMOVED = "removed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkspaceLifecycleRecord:
    session_id: str
    workspace_path: str
    base_sha: str
    expected_revision: int
    expected_state_hash: str
    disposition: WorkspaceDisposition
    state: WorkspaceLifecycleState
    requested_at: str | None
    started_at: str | None
    completed_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceEligibility:
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceCleanupOutcome:
    session_id: str
    state: WorkspaceLifecycleState
    removed: bool
    already_removed: bool
    diagnostic: str | None = None


@dataclass(frozen=True)
class WorkspaceStatusSnapshot:
    session_id: str
    session_state: str | None
    workspace_path: str | None
    registered: bool
    present: bool
    worktree_registered: bool
    base_sha: str | None
    revision: int | None
    journal_state_hash: str | None
    physical_state_hash: str | None
    disposition: str
    lifecycle_state: str
    eligible: bool
    blocking_reasons: tuple[str, ...]
    pending_outbox: bool
    collision_outbox: bool
    recoverable_command: bool
    service_status: str
    lock_held: bool
