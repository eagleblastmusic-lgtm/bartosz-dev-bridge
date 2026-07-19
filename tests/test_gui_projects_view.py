from __future__ import annotations

import sys

import pytest

from bdb_gui.projects import PreparePlan, PrepareResult

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.projects_view import ProjectsWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-projects-view"])


def plan() -> PreparePlan:
    return PreparePlan(
        alias="alpha",
        workspace_root="C:/workspaces/alpha",
        source_repo="C:/source/alpha",
        allowed_paths=("README.md", "tests/*.py"),
        python_executable=sys.executable,
        test_timeout_seconds=120,
    )


def test_widget_starts_without_plan_and_prepare_is_disabled() -> None:
    app = application()
    widget = ProjectsWidget()
    app.processEvents()

    report = widget.smoke_report()
    assert report["projects_wizard_present"] is True
    assert report["prepare_plan_required"] is True
    assert report["prepare_confirmation_required"] is True
    assert report["prepare_plan_loaded"] is False
    assert widget.plan_button.isEnabled() is True
    assert widget.prepare_button.isEnabled() is False
    assert widget.ack_checkbox.isEnabled() is False
    widget.close()


def test_build_plan_emits_only_supported_form_payload() -> None:
    app = application()
    widget = ProjectsWidget()
    widget.alias_edit.setText("alpha")
    widget.source_edit.setText("C:/source/alpha")
    widget.allowed_paths_edit.setPlainText("README.md\ntests/*.py")
    calls: list[dict[str, object]] = []
    widget.plan_requested.connect(calls.append)

    widget.plan_button.click()
    app.processEvents()

    assert calls == [
        {
            "alias": "alpha",
            "source_repo": "C:/source/alpha",
            "allowed_paths": ["README.md", "tests/*.py"],
            "python_executable": sys.executable,
            "test_timeout_seconds": 120,
        }
    ]
    widget.close()


def test_valid_plan_requires_fresh_acknowledgement_before_prepare_signal() -> None:
    app = application()
    widget = ProjectsWidget()
    calls: list[PreparePlan] = []
    widget.prepare_requested.connect(calls.append)
    current = plan()

    widget.apply_plan(current)
    assert widget.prepare_button.isEnabled() is False
    assert widget.ack_checkbox.isEnabled() is True
    widget.prepare_button.click()
    app.processEvents()
    assert calls == []

    widget.ack_checkbox.setChecked(True)
    assert widget.prepare_button.isEnabled() is True
    widget.apply_plan(current)
    assert widget.ack_checkbox.isChecked() is False
    assert widget.prepare_button.isEnabled() is False

    widget.ack_checkbox.setChecked(True)
    widget.prepare_button.click()
    app.processEvents()
    assert calls == [current]
    widget.close()


def test_form_change_invalidates_existing_plan_and_acknowledgement() -> None:
    app = application()
    widget = ProjectsWidget()
    widget.apply_plan(plan())
    widget.ack_checkbox.setChecked(True)
    assert widget.prepare_button.isEnabled() is True

    widget.alias_edit.setText("changed")
    app.processEvents()

    assert widget.plan is None
    assert widget.ack_checkbox.isChecked() is False
    assert widget.prepare_button.isEnabled() is False
    assert "PONOWNEJ WALIDACJI" in widget.plan_state.text()
    widget.close()


def test_prepare_success_and_failure_are_distinct() -> None:
    app = application()
    widget = ProjectsWidget()
    current = plan()
    widget.apply_plan(current)

    success = PrepareResult(
        plan=current,
        ok=True,
        operation_id="prepare-ok",
        project_alias="alpha",
        operator_data={"status": "prepared"},
    )
    widget.apply_prepare_result(success)
    app.processEvents()
    assert widget.plan_state.text() == "PROJEKT PRZYGOTOWANY"
    assert widget.plan is None

    widget.apply_plan(current)
    failure = PrepareResult(
        plan=current,
        ok=False,
        operation_id="prepare-failed",
        project_alias="alpha",
        operator_data={},
        error_code="operator_failed",
        error_message="source checkout is dirty",
    )
    widget.apply_prepare_result(failure)
    assert widget.plan_state.text() == "PREPARE NIEUDANY"
    assert "operator_failed" in widget.feedback_label.text()
    assert widget.plan is current
    assert widget.ack_checkbox.isChecked() is False
    widget.close()


def test_busy_state_blocks_plan_and_prepare_actions() -> None:
    application()
    widget = ProjectsWidget()
    widget.apply_plan(plan())
    widget.ack_checkbox.setChecked(True)
    widget.set_busy(True, "Praca w toku")

    assert widget.plan_button.isEnabled() is False
    assert widget.prepare_button.isEnabled() is False
    assert widget.ack_checkbox.isEnabled() is False
    widget.close()
