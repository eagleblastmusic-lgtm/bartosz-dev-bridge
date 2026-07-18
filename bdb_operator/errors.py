from __future__ import annotations

from enum import StrEnum
from typing import Any


class OperatorErrorCode(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    WORKSPACE_STATE_MISSING = "workspace_state_missing"
    WORKSPACE_STATE_INVALID = "workspace_state_invalid"
    OPERATOR_SCRIPT_MISSING = "operator_script_missing"
    EXECUTABLE_MISSING = "executable_missing"
    COMMAND_FAILED = "command_failed"
    COMMAND_TIMEOUT = "command_timeout"
    INVALID_RESPONSE = "invalid_response"
    OBSERVABILITY_CONFIG_MISSING = "observability_config_missing"
    OBSERVABILITY_CONFIG_INVALID = "observability_config_invalid"
    JOURNAL_MISSING = "journal_missing"
    JOURNAL_UNAVAILABLE = "journal_unavailable"
    LOG_READ_FAILED = "log_read_failed"
    INTERNAL_ERROR = "internal_error"


class OperatorApiError(RuntimeError):
    def __init__(
        self,
        code: OperatorErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
