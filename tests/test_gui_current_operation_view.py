from __future__ import annotations

import pytest

from bdb_gui.current_operation import CurrentOperationSnapshot, OperationDetails


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.current_operation_view import CurrentOperationWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-current-operation-view"])


def inactive_snapshot() -> CurrentOperationSnapshot:
    return CurrentOperationSnapshot(
        workspace_root="C:/workspaces/alpha",
        project_alias="alpha",
        generated_at="2026-07-18T21:01:00Z",
        active=False,
        operation=None,
        operator_operation_id="inactive-op",
    )


def active_snapshot() -> CurrentOperationSnapshot:
    return CurrentOperationSnapshot(
        workspace_root="C:/workspaces/alpha",
        project_alias="alpha",
        generated_at="2026-07-18T21:02:00Z",
        active=True,
        operation=OperationDetails(
            command_id="session-1:000003",
            session_id="session-1",
            sequence=3,
            state="executing",
            operation="multi_file_patch",
            target_path="README.md",
            profile_id="poc_pytest",
            repository_id="repo-alpha",
            session_state="active",
            workspace_revision=2,
            workspace_state_hash="sha256:" + "a" * 64,
            result_status=None,
            error_code=None,
            created_at="2026-07-18T21:00:30Z",
            updated_at="2026-07-18T21:00:45Z",
        ),
        operator_operation_id="active-op",
    )


def test_widget_starts_without_project_and_has_only_refresh_action() -> None:
    app = application()
    widget = CurrentOperationWidget()
    app.processEvents()

    report = widget.smoke_report()

    assert report["current_operation_view_present"] is True
    assert report["current_operation_read_only"] is True
    assert report["current_operation_refresh_present"] is True
    assert report["current_operation_loaded"] is False
    assert widget.refresh_button.isEnabled() is False
    assert widget.state_label.text() == "BRAK PROJEKTU"
    widget.close()


def test_inactive_snapshot_renders_explicit_empty_state() -> None:
    app = application()
    widget = CurrentOperationWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(inactive_snapshot())
    app.processEvents()

    assert widget.state_label.text() == "BRAK AKTYWNEJ OPERACJI"
    assert widget.generated_value.text() == "2026-07-18T21:01:00Z"
    assert widget.last_snapshot is not None
    assert widget.last_snapshot.read_only is True
    assert widget.last_snapshot.mutation_operations_invoked == 0
    assert widget.smoke_report()["current_operation_active"] is False
    widget.close()


def test_active_snapshot_renders_journal_projection_fields() -> None:
    app = application()
    widget = CurrentOperationWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(active_snapshot())
    app.processEvents()

    assert widget.state_label.text() == "EXECUTING"
    assert widget._values["command"].text() == "session-1:000003"
    assert widget._values["session"].text() == "session-1"
    assert widget._values["sequence"].text() == "3"
    assert widget._values["operation"].text() == "multi_file_patch"
    assert widget._values["target"].text() == "README.md"
    assert widget._values["profile"].text() == "poc_pytest"
    assert widget._values["revision"].text() == "2"
    assert widget.smoke_report()["current_operation_active"] is True
    widget.close()


def test_refresh_signal_is_explicit_and_busy_state_blocks_it() -> None:
    app = application()
    widget = CurrentOperationWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    calls: list[str] = []
    widget.refresh_requested.connect(lambda: calls.append("refresh"))

    widget.refresh_button.click()
    app.processEvents()
    assert calls == ["refresh"]

    widget.set_busy(True, "Odczyt w toku")
    assert widget.refresh_button.isEnabled() is False
    widget.refresh_button.click()
    app.processEvents()
    assert calls == ["refresh"]
    widget.close()


def test_error_snapshot_is_visible_without_mutation() -> None:
    app = application()
    widget = CurrentOperationWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    snapshot = CurrentOperationSnapshot(
        workspace_root="C:/workspaces/alpha",
        project_alias="alpha",
        generated_at=None,
        active=False,
        operation=None,
        operator_operation_id="failed-op",
        error_code="journal_missing",
        error_message="BDB Journal is missing",
    )

    widget.apply_snapshot(snapshot)
    app.processEvents()

    assert widget.state_label.text() == "ODCZYT NIEDOSTĘPNY"
    assert "journal_missing" in widget.feedback_label.text()
    assert widget.last_snapshot is not None
    assert widget.last_snapshot.mutation_operations_invoked == 0
    widget.close()
