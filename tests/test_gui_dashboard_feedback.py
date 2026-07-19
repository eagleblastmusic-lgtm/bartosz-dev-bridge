from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.dashboard import DashboardWidget  # noqa: E402
from bdb_gui.operations import ProjectStatusSnapshot  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-bdb-dashboard-feedback"])


def test_dashboard_restores_last_idle_feedback_after_global_busy_read() -> None:
    application()
    widget = DashboardWidget()
    widget.set_project("calculator2", "C:/workspaces/calculator2")
    widget.apply_status(
        ProjectStatusSnapshot(
            workspace_root="C:/workspaces/calculator2",
            project_alias="calculator2",
            overall_status="READY",
            bridge_status="RUNNING",
            bridge_pid=1234,
            bridge_pid_alive=True,
            native_armed=True,
            native_armed_until="2026-07-19T12:23:28Z",
            promoter_running=True,
            promoter_pid=5678,
            source_clean=True,
            source_head="a" * 40,
            operator_operation_id="status-op",
        )
    )

    expected = "Status pobrany tylko do odczytu. Nie wykonano żadnej operacji sterującej."
    assert widget.feedback_label.text() == expected

    widget.set_busy(True, "Odczytywanie bieżącej operacji z Journalu…")
    assert widget.feedback_label.text() == "Odczytywanie bieżącej operacji z Journalu…"

    widget.set_busy(False)
    assert widget.feedback_label.text() == expected
