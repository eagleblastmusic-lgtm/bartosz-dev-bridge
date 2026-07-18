from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from bdb_operator import OperatorApi, OperatorResponse


GUI_PROJECT_STATUS_SCHEMA = "bdb-gui-project-status-v1"
GUI_CONTROL_RESULT_SCHEMA = "bdb-gui-control-result-v1"
ControlAction = Literal["start", "stop", "rearm"]


class ProjectOperator(Protocol):
    def status(self, workspace_root: str | Path) -> OperatorResponse:
        ...

    def start(self, workspace_root: str | Path, *, arm_minutes: int = 30) -> OperatorResponse:
        ...

    def stop(self, workspace_root: str | Path) -> OperatorResponse:
        ...

    def rearm(self, workspace_root: str | Path, *, arm_minutes: int = 30) -> OperatorResponse:
        ...


@dataclass(frozen=True)
class ProjectStatusSnapshot:
    workspace_root: str
    project_alias: str | None
    overall_status: str | None
    bridge_status: str | None
    bridge_pid: int | None
    bridge_pid_alive: bool | None
    native_armed: bool | None
    native_armed_until: str | None
    promoter_running: bool | None
    promoter_pid: int | None
    source_clean: bool | None
    source_head: str | None
    operator_operation_id: str
    error_code: str | None = None
    error_message: str | None = None
    read_only: bool = True
    mutation_operations_invoked: int = 0
    schema: str = GUI_PROJECT_STATUS_SCHEMA

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @classmethod
    def from_response(
        cls,
        workspace_root: str | Path,
        response: OperatorResponse,
    ) -> "ProjectStatusSnapshot":
        root = str(Path(workspace_root).expanduser().resolve(strict=False))
        if not response.ok:
            return cls(
                workspace_root=root,
                project_alias=response.project_alias,
                overall_status=None,
                bridge_status=None,
                bridge_pid=None,
                bridge_pid_alive=None,
                native_armed=None,
                native_armed_until=None,
                promoter_running=None,
                promoter_pid=None,
                source_clean=None,
                source_head=None,
                operator_operation_id=response.operation_id,
                error_code=(response.error.code if response.error is not None else "operator_error"),
                error_message=(
                    response.error.message if response.error is not None else "Operator status failed"
                ),
            )

        try:
            data = _object(response.data, "status data")
            bridge = _object(data.get("bridge", {}), "bridge")
            native = _object(data.get("native_host", {}), "native_host")
            promoter = _object(data.get("promoter", {}), "promoter")
            alias = _optional_string(data.get("alias")) or response.project_alias
            return cls(
                workspace_root=root,
                project_alias=alias,
                overall_status=_optional_string(data.get("status")),
                bridge_status=_optional_string(bridge.get("status")),
                bridge_pid=_optional_int(bridge.get("pid")),
                bridge_pid_alive=_optional_bool(bridge.get("pid_alive")),
                native_armed=_optional_bool(native.get("armed")),
                native_armed_until=_optional_string(native.get("armed_until")),
                promoter_running=_optional_bool(promoter.get("running")),
                promoter_pid=_optional_int(promoter.get("pid")),
                source_clean=_optional_bool(data.get("source_clean")),
                source_head=_optional_string(data.get("source_head")),
                operator_operation_id=response.operation_id,
            )
        except ValueError as error:
            return cls(
                workspace_root=root,
                project_alias=response.project_alias,
                overall_status=None,
                bridge_status=None,
                bridge_pid=None,
                bridge_pid_alive=None,
                native_armed=None,
                native_armed_until=None,
                promoter_running=None,
                promoter_pid=None,
                source_clean=None,
                source_head=None,
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
            "overall_status": self.overall_status,
            "bridge": {
                "status": self.bridge_status,
                "pid": self.bridge_pid,
                "pid_alive": self.bridge_pid_alive,
            },
            "native_host": {
                "armed": self.native_armed,
                "armed_until": self.native_armed_until,
            },
            "promoter": {
                "running": self.promoter_running,
                "pid": self.promoter_pid,
            },
            "source": {
                "clean": self.source_clean,
                "head": self.source_head,
            },
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }


@dataclass(frozen=True)
class ControlResult:
    action: ControlAction
    workspace_root: str
    project_alias: str | None
    operator_operation_id: str
    ok: bool
    operator_data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    mutation_operations_invoked: int = 1
    schema: str = GUI_CONTROL_RESULT_SCHEMA

    @classmethod
    def from_response(
        cls,
        action: ControlAction,
        workspace_root: str | Path,
        response: OperatorResponse,
    ) -> "ControlResult":
        return cls(
            action=action,
            workspace_root=str(Path(workspace_root).expanduser().resolve(strict=False)),
            project_alias=response.project_alias,
            operator_operation_id=response.operation_id,
            ok=response.ok,
            operator_data=dict(response.data) if response.ok else {},
            error_code=(response.error.code if response.error is not None else None),
            error_message=(response.error.message if response.error is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "action": self.action,
            "workspace_root": self.workspace_root,
            "project_alias": self.project_alias,
            "operator_operation_id": self.operator_operation_id,
            "ok": self.ok,
            "mutation_operations_invoked": self.mutation_operations_invoked,
            "operator_data": dict(self.operator_data),
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }


class ProjectOperationsService:
    """Closed GUI service for status and explicit process-control operations."""

    def __init__(self, operator: ProjectOperator | None = None) -> None:
        self._operator = operator or OperatorApi()

    def read_status(self, workspace_root: str | Path) -> ProjectStatusSnapshot:
        response = self._operator.status(workspace_root)
        return ProjectStatusSnapshot.from_response(workspace_root, response)

    def execute(
        self,
        action: ControlAction,
        workspace_root: str | Path,
        *,
        arm_minutes: int = 30,
    ) -> ControlResult:
        if action not in {"start", "stop", "rearm"}:
            raise ValueError(f"Unsupported Control Center action: {action}")
        if isinstance(arm_minutes, bool) or not isinstance(arm_minutes, int) or not 1 <= arm_minutes <= 60:
            raise ValueError("arm_minutes must be an integer between 1 and 60")

        if action == "start":
            response = self._operator.start(workspace_root, arm_minutes=arm_minutes)
        elif action == "stop":
            response = self._operator.stop(workspace_root)
        else:
            response = self._operator.rearm(workspace_root, arm_minutes=arm_minutes)
        return ControlResult.from_response(action, workspace_root, response)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Operator {label} must be an object")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Operator string field has an invalid type")
    return value


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("Operator boolean field has an invalid type")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Operator integer field has an invalid type")
    return value
