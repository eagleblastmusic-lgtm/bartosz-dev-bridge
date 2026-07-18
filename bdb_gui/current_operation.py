from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bdb_operator import OperatorApi, OperatorResponse


GUI_CURRENT_OPERATION_SCHEMA = "bdb-gui-current-operation-v1"
GUI_OPERATION_DETAILS_SCHEMA = "bdb-gui-operation-details-v1"


class CurrentOperationOperator(Protocol):
    def current_operation(self, workspace_root: str | Path) -> OperatorResponse:
        ...


@dataclass(frozen=True)
class OperationDetails:
    command_id: str
    session_id: str
    sequence: int
    state: str
    operation: str | None
    target_path: str | None
    profile_id: str | None
    repository_id: str | None
    session_state: str | None
    workspace_revision: int | None
    workspace_state_hash: str | None
    result_status: str | None
    error_code: str | None
    created_at: str | None
    updated_at: str | None
    schema: str = GUI_OPERATION_DETAILS_SCHEMA

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "OperationDetails":
        return cls(
            command_id=_required_string(document, "command_id"),
            session_id=_required_string(document, "session_id"),
            sequence=_required_int(document, "sequence"),
            state=_required_string(document, "state"),
            operation=_optional_string(document.get("operation"), "operation"),
            target_path=_optional_string(document.get("target_path"), "target_path"),
            profile_id=_optional_string(document.get("profile_id"), "profile_id"),
            repository_id=_optional_string(document.get("repository_id"), "repository_id"),
            session_state=_optional_string(document.get("session_state"), "session_state"),
            workspace_revision=_optional_int(document.get("workspace_revision"), "workspace_revision"),
            workspace_state_hash=_optional_string(
                document.get("workspace_state_hash"), "workspace_state_hash"
            ),
            result_status=_optional_string(document.get("result_status"), "result_status"),
            error_code=_optional_string(document.get("error_code"), "error_code"),
            created_at=_optional_string(document.get("created_at"), "created_at"),
            updated_at=_optional_string(document.get("updated_at"), "updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "command_id": self.command_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "state": self.state,
            "operation": self.operation,
            "target_path": self.target_path,
            "profile_id": self.profile_id,
            "repository_id": self.repository_id,
            "session_state": self.session_state,
            "workspace_revision": self.workspace_revision,
            "workspace_state_hash": self.workspace_state_hash,
            "result_status": self.result_status,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CurrentOperationSnapshot:
    workspace_root: str
    project_alias: str | None
    generated_at: str | None
    active: bool
    operation: OperationDetails | None
    operator_operation_id: str
    error_code: str | None = None
    error_message: str | None = None
    read_only: bool = True
    mutation_operations_invoked: int = 0
    schema: str = GUI_CURRENT_OPERATION_SCHEMA

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @classmethod
    def from_response(
        cls,
        workspace_root: str | Path,
        response: OperatorResponse,
    ) -> "CurrentOperationSnapshot":
        root = str(Path(workspace_root).expanduser().resolve(strict=False))
        if not response.ok:
            return cls(
                workspace_root=root,
                project_alias=response.project_alias,
                generated_at=None,
                active=False,
                operation=None,
                operator_operation_id=response.operation_id,
                error_code=(response.error.code if response.error is not None else "operator_error"),
                error_message=(
                    response.error.message
                    if response.error is not None
                    else "Current operation read failed"
                ),
            )

        try:
            data = _object(response.data, "current operation data")
            if data.get("schema") != "bdb-current-operation-v1":
                raise ValueError("Operator current operation schema is unsupported")
            active = _required_bool(data, "active")
            operation_value = data.get("operation")
            if active:
                operation = OperationDetails.from_document(_object(operation_value, "operation"))
            else:
                if operation_value is not None:
                    raise ValueError("Inactive current operation must contain operation=null")
                operation = None
            return cls(
                workspace_root=root,
                project_alias=_optional_string(data.get("project_alias"), "project_alias")
                or response.project_alias,
                generated_at=_optional_string(data.get("generated_at"), "generated_at"),
                active=active,
                operation=operation,
                operator_operation_id=response.operation_id,
            )
        except ValueError as error:
            return cls(
                workspace_root=root,
                project_alias=response.project_alias,
                generated_at=None,
                active=False,
                operation=None,
                operator_operation_id=response.operation_id,
                error_code="invalid_operator_response",
                error_message=str(error),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workspace_root": self.workspace_root,
            "project_alias": self.project_alias,
            "ok": self.ok,
            "read_only": self.read_only,
            "mutation_operations_invoked": self.mutation_operations_invoked,
            "operator_operation_id": self.operator_operation_id,
            "generated_at": self.generated_at,
            "active": self.active,
            "operation": self.operation.to_dict() if self.operation is not None else None,
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }


class CurrentOperationService:
    """Read-only GUI adapter over OperatorApi.current_operation()."""

    def __init__(self, operator: CurrentOperationOperator | None = None) -> None:
        self._operator = operator or OperatorApi()

    def read(self, workspace_root: str | Path) -> CurrentOperationSnapshot:
        response = self._operator.current_operation(workspace_root)
        return CurrentOperationSnapshot.from_response(workspace_root, response)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Operator {label} must be an object")
    return value


def _required_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value


def _optional_string(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Operator field has an invalid string type: {key}")
    return value


def _required_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value


def _optional_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Operator field has an invalid integer type: {key}")
    return value


def _required_bool(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value
