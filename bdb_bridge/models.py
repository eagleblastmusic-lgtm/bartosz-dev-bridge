from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Operation(StrEnum):
    OPEN_READ = "open_read"
    REPLACE_EXACT_AND_TEST = "replace_exact_and_test"


class ResultStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


class BridgeErrorCode(StrEnum):
    COMMAND_ID_COLLISION = "command_id_collision"
    COMMAND_ID_MISMATCH = "command_id_mismatch"
    DIRTY_SOURCE_CHECKOUT = "dirty_source_checkout"
    GIT_ERROR = "git_error"
    INVALID_BASE_SHA = "invalid_base_sha"
    INVALID_CONFIG = "invalid_config"
    INVALID_CONTROL_REPO = "invalid_control_repo"
    INVALID_FIXTURE_REPO = "invalid_fixture_repo"
    INVALID_JSON = "invalid_json"
    INVALID_MANIFEST = "invalid_manifest"
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_PYTHON = "invalid_python"
    INVALID_RANGE = "invalid_range"
    INVALID_REVISION = "invalid_revision"
    INVALID_SESSION_ID = "invalid_session_id"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    JOURNAL_CONFLICT = "journal_conflict"
    JOURNAL_CORRUPT = "journal_corrupt"
    JOURNAL_MIGRATION_MISMATCH = "journal_migration_mismatch"
    JOURNAL_SCHEMA_UNSUPPORTED = "journal_schema_unsupported"
    MISSING_FILE = "missing_file"
    MISSING_PROTOCOL_FILE = "missing_protocol_file"
    POLICY_DENIED = "policy_denied"
    REPLACE_MISMATCH = "replace_mismatch"
    RESULT_COLLISION = "result_collision"
    RESULT_PUBLICATION_FAILED = "result_publication_failed"
    RESULT_TOO_LARGE = "result_too_large"
    SCOPE_VIOLATION = "scope_violation"
    SEQUENCE_COLLISION = "sequence_collision"
    SEQUENCE_MISMATCH = "sequence_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    STALE_REVISION = "stale_revision"
    STATE_MISMATCH = "state_mismatch"
    UNSAFE_PATH = "unsafe_path"
    UNSAFE_WORKTREE_PATH = "unsafe_worktree_path"
    UNKNOWN_BASE_SHA = "unknown_base_sha"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    WORKSPACE_EXISTS = "workspace_exists"
    SESSION_ID_COLLISION = "session_id_collision"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    INGESTION_BLOCKED = "ingestion_blocked"
    WORKSPACE_DIVERGED = "workspace_diverged"
    OPERATION_PLAN_COLLISION = "operation_plan_collision"
    EFFECT_COLLISION = "effect_collision"
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"


class CommandState(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    EFFECT_RECORDED = "effect_recorded"
    RESULT_STAGED = "result_staged"
    RESULT_PUBLISHED = "result_published"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    EXPIRED = "expired"
    POLICY_DENIED = "policy_denied"
    STALE_REVISION = "stale_revision"
    STATE_MISMATCH = "state_mismatch"
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"
    CANCELLED = "cancelled"


class SessionState(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    COMPLETING = "completing"
    COMPLETED = "completed"
    ABORTED = "aborted"
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"


COMMAND_TRANSITIONS: dict[CommandState, frozenset[CommandState]] = {
    CommandState.DISCOVERED: frozenset(
        {CommandState.VALIDATED, CommandState.REJECTED, CommandState.EXPIRED}
    ),
    CommandState.VALIDATED: frozenset(
        {
            CommandState.CLAIMED,
            CommandState.POLICY_DENIED,
            CommandState.STALE_REVISION,
            CommandState.STATE_MISMATCH,
            CommandState.CANCELLED,
        }
    ),
    CommandState.CLAIMED: frozenset(
        {
            CommandState.EXECUTING,
            CommandState.CANCELLED,
            CommandState.MANUAL_RECONCILIATION_REQUIRED,
        }
    ),
    CommandState.EXECUTING: frozenset(
        {
            CommandState.EFFECT_RECORDED,
            CommandState.RESULT_STAGED,
            CommandState.CANCELLED,
            CommandState.MANUAL_RECONCILIATION_REQUIRED,
        }
    ),
    CommandState.EFFECT_RECORDED: frozenset(
        {CommandState.RESULT_STAGED, CommandState.MANUAL_RECONCILIATION_REQUIRED}
    ),
    CommandState.RESULT_STAGED: frozenset({CommandState.RESULT_PUBLISHED}),
    CommandState.RESULT_PUBLISHED: frozenset({CommandState.ACKNOWLEDGED}),
}

TERMINAL_COMMAND_STATES: frozenset[CommandState] = frozenset(
    {
        CommandState.ACKNOWLEDGED,
        CommandState.REJECTED,
        CommandState.EXPIRED,
        CommandState.POLICY_DENIED,
        CommandState.STALE_REVISION,
        CommandState.STATE_MISMATCH,
        CommandState.MANUAL_RECONCILIATION_REQUIRED,
        CommandState.CANCELLED,
    }
)

SESSION_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset({SessionState.ACTIVE, SessionState.ABORTED}),
    SessionState.ACTIVE: frozenset(
        {
            SessionState.COMPLETING,
            SessionState.ABORTED,
            SessionState.MANUAL_RECONCILIATION_REQUIRED,
        }
    ),
    SessionState.COMPLETING: frozenset(
        {
            SessionState.COMPLETED,
            SessionState.ABORTED,
            SessionState.MANUAL_RECONCILIATION_REQUIRED,
        }
    ),
}

TERMINAL_SESSION_STATES: frozenset[SessionState] = frozenset(
    {
        SessionState.COMPLETED,
        SessionState.ABORTED,
        SessionState.MANUAL_RECONCILIATION_REQUIRED,
    }
)


def validate_command_transition(current: CommandState, new: CommandState) -> None:
    from .protocol import BridgeError

    if current in TERMINAL_COMMAND_STATES:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Command state {current.value} is terminal",
        )
    allowed = COMMAND_TRANSITIONS.get(current, frozenset())
    if new not in allowed:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Invalid command transition {current.value} -> {new.value}",
        )


def validate_session_transition(current: SessionState, new: SessionState) -> None:
    from .protocol import BridgeError

    if current in TERMINAL_SESSION_STATES:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Session state {current.value} is terminal",
        )
    allowed = SESSION_TRANSITIONS.get(current, frozenset())
    if new not in allowed:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Invalid session transition {current.value} -> {new.value}",
        )


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    repository_id: str
    base_sha: str
    state: SessionState
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CommandRecord:
    command_id: str
    session_id: str
    sequence: int
    command_sha256: str
    command_json: str
    command_commit_sha: str | None
    state: CommandState
    expected_revision: int | None
    expected_state_hash: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceRecord:
    session_id: str
    workspace_path: str
    base_sha: str
    revision: int
    state_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ResultRecord:
    command_id: str
    session_id: str
    sequence: int
    status: str
    error_code: str | None
    result_sha256: str
    result_json: str
    remote_path: str
    created_at: str


@dataclass(frozen=True)
class JournalEvent:
    event_id: int
    session_id: str | None
    command_id: str | None
    event_type: str
    payload_json: str | None
    created_at: str


SCHEDULER_PREDECESSOR_DONE_STATES: frozenset[CommandState] = frozenset(
    {
        CommandState.RESULT_STAGED,
        CommandState.RESULT_PUBLISHED,
        CommandState.ACKNOWLEDGED,
        CommandState.REJECTED,
        CommandState.EXPIRED,
        CommandState.POLICY_DENIED,
        CommandState.STALE_REVISION,
        CommandState.STATE_MISMATCH,
        CommandState.MANUAL_RECONCILIATION_REQUIRED,
        CommandState.CANCELLED,
    }
)

SCHEDULER_PREDECESSOR_BLOCKING_STATES: frozenset[CommandState] = frozenset(
    {
        CommandState.DISCOVERED,
        CommandState.VALIDATED,
        CommandState.CLAIMED,
        CommandState.EXECUTING,
        CommandState.EFFECT_RECORDED,
    }
)


@dataclass(frozen=True)
class TransportRetryRecord:
    source_id: str
    last_observed_sha: str | None
    attempt_count: int
    next_attempt_at: str | None
    last_error: str | None
    last_success_at: str | None
    updated_at: str


@dataclass(frozen=True)
class SessionIngestionRecord:
    session_id: str
    source_path: str
    manifest_commit_sha: str
    raw_sha256: str
    manifest_sha256: str
    manifest_json: str
    created_remote_at: str
    expires_at: str
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class CommandIngestionRecord:
    command_id: str
    source_id: str
    snapshot_sha: str
    source_path: str
    document_commit_sha: str
    raw_sha256: str
    created_remote_at: str | None
    expires_at: str | None
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class IngestionIssue:
    issue_id: int
    source_id: str
    source_path: str
    snapshot_sha: str
    document_commit_sha: str | None
    raw_sha256: str
    session_id: str | None
    command_id: str | None
    error_code: str
    detail: str
    blocking: bool
    created_at: str


@dataclass(frozen=True)
class IngestionReport:
    manifests_recorded: int
    commands_discovered: int
    commands_validated: int
    commands_rejected: int
    commands_expired: int
    issues_recorded: int
    blocking_issues: bool


@dataclass(frozen=True)
class PollReport:
    transport_called: bool
    transport_skipped: bool
    transport_succeeded: bool
    snapshot_sha: str | None
    ingestion: IngestionReport | None
    error_code: str | None
    error_message: str | None


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .journal_ingestion import CollisionError


@dataclass(frozen=True)
class PromotionOutcome:
    promoted_count: int
    issues_created: int
    blocking_collisions: tuple[CollisionError, ...]


@dataclass(frozen=True)
class OperationPlanRecord:
    command_id: str
    session_id: str
    operation: str
    target_path: str
    profile_id: str
    expected_revision: int
    expected_state_hash: str | None
    workspace_revision_before: int
    workspace_state_hash_before: str
    before_content: bytes
    before_content_sha256: str
    planned_after_content: bytes
    planned_after_content_sha256: str
    planned_after_state_hash: str
    plan_sha256: str
    created_at: str


@dataclass(frozen=True)
class OperationEffectRecord:
    command_id: str
    session_id: str
    plan_sha256: str
    target_path: str
    workspace_revision_before: int
    workspace_revision_after: int
    workspace_state_hash_before: str
    workspace_state_hash_after: str
    before_content_sha256: str
    after_content_sha256: str
    effect_sha256: str
    recorded_at: str


class RecoveryDecision(StrEnum):
    EXECUTE = "execute"
    RECOVER_PLANNED_AFTER = "recover_planned_after"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    DIVERGED = "diverged"


@dataclass(frozen=True)
class ProfileRunOutcome:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str
    error_code: str | None
    summary: str
    workspace_revision_before: int
    workspace_revision_after: int
    workspace_state_hash_before: str
    workspace_state_hash_after: str
    changed_files: list[str]
    diff: str
    profile_run: ProfileRunOutcome | None = None
    manual_reconciliation_details: dict[str, Any] | None = None
