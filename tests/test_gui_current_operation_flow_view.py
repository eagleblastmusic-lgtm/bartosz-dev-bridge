from __future__ import annotations

import pytest

from bdb_gui.current_operation import CurrentOperationSnapshot, OperationDetails


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.current_operation_view import CurrentOperationWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-current-operation-flow-view"])


def snapshot(*, state: str, result_status: str | None, error_code: str | None) -> CurrentOperationSnapshot:
    return CurrentOperationSnapshot(
        workspace_root="C:/workspaces/alpha",
        project_alias="alpha",
        generated_at="2026-07-19T19:30:00Z",
        active=True,
        operation=OperationDetails(
            command_id="session-1:000001",
            session_id="session-1",
            sequence=1,
            state=state,
            operation="multi_file_patch",
            target_path="README.md",
            profile_id="poc_pytest",
            repository_id="repo-alpha",
            session_state="completed" if state == "completed" else "active",
            workspace_revision=2,
            workspace_state_hash="sha256:" + "b" * 64,
            result_status=result_status,
            error_code=error_code,
            created_at="2026-07-19T19:29:00Z",
            updated_at="2026-07-19T19:29:30Z",
        ),
        operator_operation_id="flow-op",
    )


def test_executing_snapshot_renders_six_step_read_only_flow() -> None:
    app = application()
    widget = CurrentOperationWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot(state="executing", result_status=None, error_code=None))
    app.processEvents()

    report = widget.smoke_report()
    assert report["operation_flow_present"] is True
    assert report["operation_flow_status"] == "active"
    assert widget._flow_rows["editing"].status_label.text() == "TRWA"
    assert widget._flow_rows["testing"].status_label.text() == "OCZEKUJE"
    assert "executing" in widget.flow_summary_label.text()
    widget.close()


def test_completed_snapshot_marks_every_flow_step_ready() -> None:
    app = application()
    widget = CurrentOperationWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot(state="completed", result_status="success", error_code=None))
    app.processEvents()

    assert widget.smoke_report()["operation_flow_status"] == "success"
    assert all(row.status_label.text() == "GOTOWE" for row in widget._flow_rows.values())
    widget.close()


def test_failed_snapshot_surfaces_error_without_claiming_success() -> None:
    app = application()
    widget = CurrentOperationWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot(state="failed", result_status="failed", error_code="test_failed"))
    app.processEvents()

    assert widget.smoke_report()["operation_flow_status"] == "failed"
    assert widget._flow_rows["testing"].status_label.text() == "BŁĄD"
    assert widget._flow_rows["completion"].status_label.text() == "BŁĄD"
    assert "test_failed" in widget.flow_summary_label.text()
    widget.close()
