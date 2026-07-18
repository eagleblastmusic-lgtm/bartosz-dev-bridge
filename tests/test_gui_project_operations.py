from __future__ import annotations

from pathlib import Path

import pytest

from bdb_gui.operations import (
    GUI_CONTROL_RESULT_SCHEMA,
    GUI_PROJECT_STATUS_SCHEMA,
    ProjectOperationsService,
)
from bdb_operator.models import OperatorError, OperatorResponse


class FakeProjectOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None]] = []
        self.status_response = OperatorResponse.success(
            "status",
            project_alias="alpha",
            operation_id="status-op",
            data={
                "alias": "alpha",
                "status": "RUNNING",
                "bridge": {"status": "RUNNING", "pid": 1201, "pid_alive": True},
                "native_host": {"armed": True, "armed_until": "2026-07-18T22:00:00Z"},
                "promoter": {"running": True, "pid": 1202},
                "source_clean": True,
                "source_head": "a" * 40,
            },
        )
        self.start_response = OperatorResponse.success(
            "start",
            project_alias="alpha",
            operation_id="start-op",
            data={"status": "RUNNING"},
        )
        self.stop_response = OperatorResponse.success(
            "stop",
            project_alias="alpha",
            operation_id="stop-op",
            data={"status": "OFFLINE"},
        )
        self.rearm_response = OperatorResponse.success(
            "rearm",
            project_alias="alpha",
            operation_id="rearm-op",
            data={"armed": True},
        )

    def status(self, workspace_root: str | Path) -> OperatorResponse:
        self.calls.append(("status", str(workspace_root), None))
        return self.status_response

    def start(self, workspace_root: str | Path, *, arm_minutes: int = 30) -> OperatorResponse:
        self.calls.append(("start", str(workspace_root), arm_minutes))
        return self.start_response

    def stop(self, workspace_root: str | Path) -> OperatorResponse:
        self.calls.append(("stop", str(workspace_root), None))
        return self.stop_response

    def rearm(self, workspace_root: str | Path, *, arm_minutes: int = 30) -> OperatorResponse:
        self.calls.append(("rearm", str(workspace_root), arm_minutes))
        return self.rearm_response


def test_status_projection_is_explicitly_read_only(tmp_path: Path) -> None:
    operator = FakeProjectOperator()
    service = ProjectOperationsService(operator)
    workspace = tmp_path / "alpha"

    snapshot = service.read_status(workspace)

    assert snapshot.schema == GUI_PROJECT_STATUS_SCHEMA
    assert snapshot.ok is True
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0
    assert snapshot.project_alias == "alpha"
    assert snapshot.overall_status == "RUNNING"
    assert snapshot.bridge_status == "RUNNING"
    assert snapshot.bridge_pid == 1201
    assert snapshot.bridge_pid_alive is True
    assert snapshot.native_armed is True
    assert snapshot.promoter_running is True
    assert snapshot.source_clean is True
    assert snapshot.source_head == "a" * 40
    assert operator.calls == [("status", str(workspace), None)]

    document = snapshot.to_dict()
    assert document["schema"] == GUI_PROJECT_STATUS_SCHEMA
    assert document["read_only"] is True
    assert document["mutation_operations_invoked"] == 0
    assert document["error"] is None


@pytest.mark.parametrize(
    ("action", "minutes", "expected_call", "operation_id"),
    [
        ("start", 25, "start", "start-op"),
        ("stop", 30, "stop", "stop-op"),
        ("rearm", 45, "rearm", "rearm-op"),
    ],
)
def test_closed_control_catalog_invokes_exact_operator_method(
    tmp_path: Path,
    action: str,
    minutes: int,
    expected_call: str,
    operation_id: str,
) -> None:
    operator = FakeProjectOperator()
    service = ProjectOperationsService(operator)
    workspace = tmp_path / "alpha"

    result = service.execute(action, workspace, arm_minutes=minutes)  # type: ignore[arg-type]

    assert result.schema == GUI_CONTROL_RESULT_SCHEMA
    assert result.ok is True
    assert result.action == action
    assert result.operator_operation_id == operation_id
    assert result.mutation_operations_invoked == 1
    expected_minutes = None if action == "stop" else minutes
    assert operator.calls == [(expected_call, str(workspace), expected_minutes)]
    document = result.to_dict()
    assert document["schema"] == GUI_CONTROL_RESULT_SCHEMA
    assert document["mutation_operations_invoked"] == 1
    assert document["error"] is None


def test_unsupported_action_is_rejected_before_operator_call(tmp_path: Path) -> None:
    operator = FakeProjectOperator()
    service = ProjectOperationsService(operator)

    with pytest.raises(ValueError, match="Unsupported Control Center action"):
        service.execute("shell", tmp_path / "alpha")  # type: ignore[arg-type]

    assert operator.calls == []


@pytest.mark.parametrize("value", [0, 61, True, 3.5, "30"])
def test_arm_minutes_is_bounded_before_mutation(tmp_path: Path, value: object) -> None:
    operator = FakeProjectOperator()
    service = ProjectOperationsService(operator)

    with pytest.raises(ValueError, match="arm_minutes"):
        service.execute("start", tmp_path / "alpha", arm_minutes=value)  # type: ignore[arg-type]

    assert operator.calls == []


def test_operator_failure_is_preserved_in_control_result(tmp_path: Path) -> None:
    operator = FakeProjectOperator()
    operator.stop_response = OperatorResponse.failure(
        "stop",
        project_alias="alpha",
        operation_id="stop-failed",
        error=OperatorError(code="service_stale", message="Bridge status is STALE"),
    )
    service = ProjectOperationsService(operator)

    result = service.execute("stop", tmp_path / "alpha")

    assert result.ok is False
    assert result.error_code == "service_stale"
    assert result.error_message == "Bridge status is STALE"
    assert result.operator_data == {}
    assert result.mutation_operations_invoked == 1


def test_invalid_status_shape_becomes_typed_read_error(tmp_path: Path) -> None:
    operator = FakeProjectOperator()
    operator.status_response = OperatorResponse.success(
        "status",
        project_alias="alpha",
        operation_id="bad-status",
        data={"bridge": "not-an-object"},
    )
    service = ProjectOperationsService(operator)

    snapshot = service.read_status(tmp_path / "alpha")

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0
