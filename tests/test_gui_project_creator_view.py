from __future__ import annotations

import sys
from pathlib import Path

import pytest


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.project_creator import DEFAULT_ALLOWED_PATHS  # noqa: E402
from bdb_gui.project_creator_view import ProjectCreatorDialog  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-project-creator-view"])


def test_new_project_dialog_collects_one_confirmed_plan(tmp_path: Path) -> None:
    app = application()
    dialog = ProjectCreatorDialog(default_projects_root=tmp_path)
    captured: list[dict[str, object]] = []
    dialog.submitted.connect(captured.append)

    assert dialog.mode_combo.currentData() == "new"
    assert dialog.source_edit.isHidden() is True
    assert dialog.visibility_combo.isHidden() is False
    assert dialog.submit_button.isEnabled() is False
    assert dialog.allowed_paths_edit.toPlainText().splitlines() == list(DEFAULT_ALLOWED_PATHS)

    dialog.name_edit.setText("calculator")
    dialog.prompt_edit.setPlainText("Create a tested calculator")
    dialog.python_edit.setText(sys.executable)
    dialog.confirm_checkbox.setChecked(True)
    app.processEvents()

    assert dialog.alias_edit.text() == "calculator"
    assert dialog.submit_button.isEnabled() is True
    dialog.submit_button.click()
    app.processEvents()

    assert len(captured) == 1
    payload = captured[0]
    assert payload["mode"] == "new"
    assert payload["project_name"] == "calculator"
    assert payload["alias"] == "calculator"
    assert payload["github_visibility"] == "private"
    assert payload["prompt"] == "Create a tested calculator"
    assert payload["auto_send"] is True
    assert payload["allowed_paths"] == list(DEFAULT_ALLOWED_PATHS)


def test_allowed_paths_payload_strips_blank_lines(tmp_path: Path) -> None:
    application()
    dialog = ProjectCreatorDialog(default_projects_root=tmp_path)

    dialog.allowed_paths_edit.setPlainText("src/**\n\n tests/**  \n")

    assert dialog.payload()["allowed_paths"] == ["src/**", "tests/**"]
    dialog.close()


def test_existing_mode_exposes_local_or_github_source(tmp_path: Path) -> None:
    application()
    dialog = ProjectCreatorDialog(default_projects_root=tmp_path)

    dialog.mode_combo.setCurrentIndex(1)

    assert dialog.mode_combo.currentData() == "existing"
    assert dialog.source_edit.isHidden() is False
    assert dialog.visibility_combo.isHidden() is True
    assert dialog.submit_button.text() == "Podłącz i uruchom"
    dialog.close()
