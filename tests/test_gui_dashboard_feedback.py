from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.dashboard import DashboardWidget  # noqa: E402
from bdb_gui.operations import ControlResult, ProjectStatusSnapshot  # noqa: E402


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


def test_dashboard_preserves_control_result_after_confirming_status() -> None:
    application()
    widget = DashboardWidget()
    widget.set_project("calculator2", "C:/workspaces/calculator2")
    widget.apply_control_result(
        ControlResult(
            action="rearm",
            workspace_root="C:/workspaces/calculator2",
            project_alias="calculator2",
            operator_operation_id="rearm-op",
            ok=True,
        )
    )

    assert widget.feedback_label.text() == (
        "Native Host ponownie uzbrojony. "
        "Trwa odświeżanie statusu potwierdzającego wynik."
    )

    widget.set_busy(True, "Pobieranie statusu projektu tylko do odczytu…")
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

    expected = "Native Host ponownie uzbrojony. Status potwierdzający: READY."
    assert widget.feedback_label.text() == expected

    widget.set_busy(True, "Odczytywanie bieżącej operacji z Journalu…")
    widget.set_busy(False)
    assert widget.feedback_label.text() == expected


def test_dashboard_hides_historical_arm_deadline_when_disarmed() -> None:
    application()
    widget = DashboardWidget()
    widget.set_project("calculator2", "C:/workspaces/calculator2")
    historical_deadline = "2026-07-19T12:23:28Z"

    widget.apply_status(
        ProjectStatusSnapshot(
            workspace_root="C:/workspaces/calculator2",
            project_alias="calculator2",
            overall_status="OFFLINE",
            bridge_status="OFFLINE",
            bridge_pid=None,
            bridge_pid_alive=False,
            native_armed=False,
            native_armed_until=historical_deadline,
            promoter_running=False,
            promoter_pid=None,
            source_clean=True,
            source_head="a" * 40,
            operator_operation_id="status-op",
        )
    )

    assert widget.native_card.value_label.text() == "ROZBROJONY"
    assert widget.native_card.detail_label.text() == "Brak aktywnego terminu uzbrojenia"
    assert historical_deadline not in widget.native_card.detail_label.text()
