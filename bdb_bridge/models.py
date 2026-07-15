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
