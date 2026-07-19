from __future__ import annotations

from bdb_gui.current_operation import OperationDetails
from bdb_gui.operation_flow import build_operation_flow, empty_operation_flow


def operation(**overrides: object) -> OperationDetails:
    values: dict[str, object] = {
        "command_id": "session-1:000001",
        "session_id": "session-1",
        "sequence": 1,
        "state": "executing",
        "operation": "multi_file_patch",
        "target_path": "README.md",
        "profile_id": "poc_pytest",
        "repository_id": "repo-alpha",
        "session_state": "active",
        "workspace_revision": 1,
        "workspace_state_hash": "sha256:" + "a" * 64,
        "result_status": None,
        "error_code": None,
        "created_at": "2026-07-19T19:00:00Z",
        "updated_at": "2026-07-19T19:00:10Z",
    }
    values.update(overrides)
    return OperationDetails(**values)  # type: ignore[arg-type]


def test_executing_operation_marks_editing_active_without_inventing_result() -> None:
    flow = build_operation_flow(operation())

    assert flow.overall_status == "active"
    assert flow.status_for("accepted") == "success"
    assert flow.status_for("workspace") == "success"
    assert flow.status_for("editing") == "active"
    assert flow.status_for("testing") == "pending"
    assert flow.status_for("result") == "pending"
    assert flow.status_for("completion") == "pending"
    assert "executing" in flow.summary


def test_successful_published_result_marks_flow_complete() -> None:
    flow = build_operation_flow(
        operation(
            state="completed",
            session_state="completed",
            result_status="success",
            workspace_revision=3,
        )
    )

    assert flow.overall_status == "success"
    assert all(step.status == "success" for step in flow.steps)
    assert "powodzeniem" in flow.summary


def test_failed_result_is_visible_and_does_not_claim_rollback_or_promotion() -> None:
    flow = build_operation_flow(
        operation(
            state="failed",
            session_state="failed",
            result_status="failed",
            error_code="test_failed",
        )
    )

    assert flow.overall_status == "failed"
    assert flow.status_for("editing") == "failed"
    assert flow.status_for("testing") == "failed"
    assert flow.status_for("result") == "failed"
    assert flow.status_for("completion") == "failed"
    assert "test_failed" in flow.summary
    assert all("rollback" not in step.detail.lower() for step in flow.steps)
    assert all("promotion" not in step.detail.lower() for step in flow.steps)


def test_empty_flow_is_explicitly_pending() -> None:
    flow = empty_operation_flow()

    assert flow.overall_status == "pending"
    assert len(flow.steps) == 6
    assert all(step.status == "pending" for step in flow.steps)
