from __future__ import annotations

from pathlib import Path

from bdb_gui.current_operation import (
    GUI_CURRENT_OPERATION_SCHEMA,
    GUI_OPERATION_DETAILS_SCHEMA,
    CurrentOperationService,
)
from bdb_operator.models import OperatorError, OperatorResponse


class FakeCurrentOperationOperator:
    def __init__(self, response: OperatorResponse) -> None:
        self.response = response
        self.calls: list[str] = []

    def current_operation(self, workspace_root: str | Path) -> OperatorResponse:
        self.calls.append(str(workspace_root))
        return self.response


def inactive_response() -> OperatorResponse:
    return OperatorResponse.success(
        "current_operation",
        project_alias="alpha",
        operation_id="current-op-1",
        data={
            "schema": "bdb-current-operation-v1",
            "project_alias": "alpha",
            "generated_at": "2026-07-18T21:00:00Z",
            "active": False,
            "operation": None,
        },
    )


def active_response() -> OperatorResponse:
    return OperatorResponse.success(
        "current_operation",
        project_alias="alpha",
        operation_id="current-op-2",
        data={
            "schema": "bdb-current-operation-v1",
            "project_alias": "alpha",
            "generated_at": "2026-07-18T21:01:00Z",
            "active": True,
            "operation": {
                "command_id": "session-1:000003",
                "session_id": "session-1",
                "sequence": 3,
                "state": "executing",
                "operation": "multi_file_patch",
                "target_path": None,
                "profile_id": "poc_pytest",
                "repository_id": "repo-alpha",
                "session_state": "active",
                "workspace_revision": 2,
                "workspace_state_hash": "sha256:" + "a" * 64,
                "result_status": None,
                "error_code": None,
                "created_at": "2026-07-18T21:00:30Z",
                "updated_at": "2026-07-18T21:00:45Z",
            },
        },
    )


def test_inactive_projection_is_read_only(tmp_path: Path) -> None:
    operator = FakeCurrentOperationOperator(inactive_response())
    snapshot = CurrentOperationService(operator).read(tmp_path / "alpha")

    assert snapshot.schema == GUI_CURRENT_OPERATION_SCHEMA
    assert snapshot.ok is True
    assert snapshot.active is False
    assert snapshot.operation is None
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0
    assert snapshot.operator_operation_id == "current-op-1"
    assert operator.calls == [str(tmp_path / "alpha")]
    assert snapshot.to_dict()["operation"] is None


def test_active_projection_preserves_operation_fields(tmp_path: Path) -> None:
    snapshot = CurrentOperationService(FakeCurrentOperationOperator(active_response())).read(
        tmp_path / "alpha"
    )

    assert snapshot.ok is True
    assert snapshot.active is True
    assert snapshot.operation is not None
    operation = snapshot.operation
    assert operation.schema == GUI_OPERATION_DETAILS_SCHEMA
    assert operation.command_id == "session-1:000003"
    assert operation.session_id == "session-1"
    assert operation.sequence == 3
    assert operation.state == "executing"
    assert operation.operation == "multi_file_patch"
    assert operation.profile_id == "poc_pytest"
    assert operation.workspace_revision == 2
    assert operation.result_status is None
    document = snapshot.to_dict()
    assert document["read_only"] is True
    assert document["mutation_operations_invoked"] == 0
    assert document["operation"]["schema"] == GUI_OPERATION_DETAILS_SCHEMA


def test_operator_error_is_preserved(tmp_path: Path) -> None:
    response = OperatorResponse.failure(
        "current_operation",
        project_alias="alpha",
        operation_id="current-failed",
        error=OperatorError(code="journal_missing", message="BDB Journal is missing"),
    )
    snapshot = CurrentOperationService(FakeCurrentOperationOperator(response)).read(
        tmp_path / "alpha"
    )

    assert snapshot.ok is False
    assert snapshot.error_code == "journal_missing"
    assert snapshot.error_message == "BDB Journal is missing"
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0


def test_invalid_schema_becomes_typed_read_error(tmp_path: Path) -> None:
    response = inactive_response()
    response = OperatorResponse.success(
        "current_operation",
        project_alias="alpha",
        operation_id="bad-schema",
        data={**response.data, "schema": "unexpected-v9"},
    )
    snapshot = CurrentOperationService(FakeCurrentOperationOperator(response)).read(
        tmp_path / "alpha"
    )

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
    assert "schema" in (snapshot.error_message or "")


def test_active_projection_requires_operation_object(tmp_path: Path) -> None:
    response = OperatorResponse.success(
        "current_operation",
        project_alias="alpha",
        data={
            "schema": "bdb-current-operation-v1",
            "project_alias": "alpha",
            "generated_at": "2026-07-18T21:01:00Z",
            "active": True,
            "operation": None,
        },
    )
    snapshot = CurrentOperationService(FakeCurrentOperationOperator(response)).read(
        tmp_path / "alpha"
    )

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
