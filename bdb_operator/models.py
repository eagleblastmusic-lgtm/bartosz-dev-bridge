from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


OPERATOR_RESPONSE_SCHEMA = "bdb-operator-response-v1"
OPERATOR_PROJECT_SCHEMA = "bdb-operator-project-v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class OperatorError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class OperatorResponse:
    operation: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: OperatorError | None = None
    project_alias: str | None = None
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    generated_at: str = field(default_factory=utc_now_iso)
    schema: str = OPERATOR_RESPONSE_SCHEMA

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("Successful OperatorResponse cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("Failed OperatorResponse requires an error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "operation": self.operation,
            "ok": self.ok,
            "generated_at": self.generated_at,
            "project_alias": self.project_alias,
            "data": dict(self.data),
            "error": self.error.to_dict() if self.error is not None else None,
        }

    @classmethod
    def success(
        cls,
        operation: str,
        *,
        data: dict[str, Any],
        project_alias: str | None = None,
        operation_id: str | None = None,
    ) -> "OperatorResponse":
        values: dict[str, Any] = {
            "operation": operation,
            "ok": True,
            "data": data,
            "project_alias": project_alias,
        }
        if operation_id is not None:
            values["operation_id"] = operation_id
        return cls(**values)

    @classmethod
    def failure(
        cls,
        operation: str,
        *,
        error: OperatorError,
        project_alias: str | None = None,
        operation_id: str | None = None,
    ) -> "OperatorResponse":
        values: dict[str, Any] = {
            "operation": operation,
            "ok": False,
            "error": error,
            "project_alias": project_alias,
        }
        if operation_id is not None:
            values["operation_id"] = operation_id
        return cls(**values)
