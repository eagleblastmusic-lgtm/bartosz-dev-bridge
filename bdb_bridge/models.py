from __future__ import annotations

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
    MISSING_FILE = "missing_file"
    MISSING_PROTOCOL_FILE = "missing_protocol_file"
    POLICY_DENIED = "policy_denied"
    REPLACE_MISMATCH = "replace_mismatch"
    RESULT_PUBLICATION_FAILED = "result_publication_failed"
    RESULT_TOO_LARGE = "result_too_large"
    SCOPE_VIOLATION = "scope_violation"
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
