from __future__ import annotations

from pathlib import Path

import pytest

from bdb_gui.session_history import SessionHistoryService
from bdb_operator import OperatorApi
from session_projection_fixture import workspace_fixture

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402
from bdb_gui.session_history_view import SessionHistoryWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["session-view-diagnostic"])


def prepared_widget(tmp_path: Path) -> tuple[SessionHistoryWidget, list[str], object]:
    root, _, _, _ = workspace_fixture(tmp_path)
    snapshot = SessionHistoryService(
        OperatorApi(repo_root=tmp_path, platform_name="posix")
    ).read(root, limit=10)
    opened: list[str] = []
    widget = SessionHistoryWidget(path_opener=lambda path: not opened.append(path))
    widget.set_project("sample", str(root))
    widget.apply_snapshot(snapshot)
    application().processEvents()
    return widget, opened, snapshot


def test_diag_snapshot_renders_and_selects_first_row(tmp_path: Path) -> None:
    widget, opened, snapshot = prepared_widget(tmp_path)
    assert snapshot.ok is True
    assert opened == []
    assert widget.session_count == 2
    assert widget.table.currentRow() == 0
    assert widget._selected_attempt is not None
    widget.close()


def test_diag_action_buttons_are_enabled(tmp_path: Path) -> None:
    widget, _, snapshot = prepared_widget(tmp_path)
    selected = snapshot.sessions[0].latest_attempt
    assert selected is not None
    assert selected.result_file.valid is True
    assert selected.receipt_file.valid is True
    assert widget.open_result_button.isEnabled() is True
    assert widget.open_receipt_button.isEnabled() is True
    assert widget.open_folder_button.isEnabled() is True
    widget.close()


def test_diag_result_button_opens_validated_file(tmp_path: Path) -> None:
    widget, opened, snapshot = prepared_widget(tmp_path)
    selected = snapshot.sessions[0].latest_attempt
    assert selected is not None
    widget.open_result_button.click()
    application().processEvents()
    assert opened == [selected.result_file.path]
    widget.close()


def test_diag_receipt_button_opens_validated_file(tmp_path: Path) -> None:
    widget, opened, snapshot = prepared_widget(tmp_path)
    selected = snapshot.sessions[0].latest_attempt
    assert selected is not None
    widget.open_receipt_button.click()
    application().processEvents()
    assert opened == [selected.receipt_file.path]
    widget.close()


def test_diag_folder_button_opens_validated_parent(tmp_path: Path) -> None:
    widget, opened, snapshot = prepared_widget(tmp_path)
    selected = snapshot.sessions[0].latest_attempt
    assert selected is not None
    widget.open_folder_button.click()
    application().processEvents()
    assert opened == [str(Path(selected.result_file.path or "").parent)]
    widget.close()
