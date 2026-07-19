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
    return QApplication.instance() or QApplication(["test-session-history-view"])


def test_session_history_view_opens_only_explicit_validated_paths(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    snapshot = SessionHistoryService(
        OperatorApi(repo_root=tmp_path, platform_name="posix")
    ).read(root, limit=10)
    opened: list[str] = []
    widget = SessionHistoryWidget(path_opener=lambda path: opened.append(path) is None)
    widget.set_project("sample", str(root))

    widget.apply_snapshot(snapshot)
    application().processEvents()

    assert opened == []
    assert widget.session_count == 2
    assert widget.open_result_button.isEnabled() is True
    assert widget.open_receipt_button.isEnabled() is True
    assert widget.open_folder_button.isEnabled() is True

    selected = snapshot.sessions[0].latest_attempt
    assert selected is not None
    widget.open_result_button.click()
    widget.open_receipt_button.click()
    widget.open_folder_button.click()
    application().processEvents()

    assert opened[0] == selected.result_file.path
    assert opened[1] == selected.receipt_file.path
    assert opened[2] == str(Path(selected.result_file.path or "").parent)
    widget.close()


def test_failed_unpromoted_session_disables_receipt_action(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    snapshot = SessionHistoryService(
        OperatorApi(repo_root=tmp_path, platform_name="posix")
    ).read(root, limit=10)
    widget = SessionHistoryWidget(path_opener=lambda path: True)
    widget.set_project("sample", str(root))
    widget.apply_snapshot(snapshot)
    application().processEvents()

    widget.table.selectRow(1)
    application().processEvents()

    assert widget.open_result_button.isEnabled() is True
    assert widget.open_receipt_button.isEnabled() is False
    assert widget.open_folder_button.isEnabled() is True
    assert "rolled_back" in widget.details.toPlainText()
    assert "repair_relationship_inferred" in widget.details.toPlainText()
    widget.close()


def test_empty_session_history_smoke_is_read_only() -> None:
    widget = SessionHistoryWidget(path_opener=lambda path: True)
    report = widget.smoke_report()

    assert report["session_history_view_present"] is True
    assert report["session_history_read_only"] is True
    assert report["session_history_loaded"] is False
    assert report["session_repair_relationships_inferred"] is False
    assert widget.open_result_button.isEnabled() is False
    assert widget.open_receipt_button.isEnabled() is False
    widget.close()
