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


def test_session_history_view_shows_explicit_repair_chain_and_opens_only_validated_paths(tmp_path: Path) -> None:
    app = application()
    root, _, _, _ = workspace_fixture(tmp_path)
    snapshot = SessionHistoryService(
        OperatorApi(repo_root=tmp_path, platform_name="posix")
    ).read(root, limit=10)
    opened: list[str] = []
    widget = SessionHistoryWidget(path_opener=lambda path: opened.append(path) is None)
    widget.set_project("sample", str(root))

    widget.apply_snapshot(snapshot)
    app.processEvents()

    assert opened == []
    assert widget.session_count == 2
    assert widget.repair_group_count == 1
    assert widget.table.item(0, 7).text() == "NAPRAWA"
    assert widget.table.item(1, 7).text() == "START"
    assert "repair_group" in widget.details.toPlainText()
    assert '"verified": true' in widget.details.toPlainText()
    assert "jawnego correlation ID" in widget.feedback_label.text()
    assert widget.open_result_button.isEnabled() is True
    assert widget.open_receipt_button.isEnabled() is True
    assert widget.open_folder_button.isEnabled() is True

    selected = snapshot.sessions[0].latest_attempt
    assert selected is not None
    widget.open_result_button.click()
    widget.open_receipt_button.click()
    widget.open_folder_button.click()
    app.processEvents()

    assert opened[0] == selected.result_file.path
    assert opened[1] == selected.receipt_file.path
    assert opened[2] == str(Path(selected.result_file.path or "").parent)
    report = widget.smoke_report()
    assert report["session_repair_group_count"] == 1
    assert report["session_verified_repair_group_count"] == 1
    assert report["session_repair_relationships_explicit_only"] is True
    widget.close()


def test_failed_initial_session_disables_receipt_and_shows_verified_group(tmp_path: Path) -> None:
    app = application()
    root, _, _, _ = workspace_fixture(tmp_path)
    snapshot = SessionHistoryService(
        OperatorApi(repo_root=tmp_path, platform_name="posix")
    ).read(root, limit=10)
    widget = SessionHistoryWidget(path_opener=lambda path: True)
    widget.set_project("sample", str(root))
    widget.apply_snapshot(snapshot)
    app.processEvents()

    widget.table.selectRow(1)
    app.processEvents()

    assert widget.table.item(1, 7).text() == "START"
    assert widget.open_result_button.isEnabled() is True
    assert widget.open_receipt_button.isEnabled() is False
    assert widget.open_folder_button.isEnabled() is True
    assert "rolled_back" in widget.details.toPlainText()
    assert '"role": "initial"' in widget.details.toPlainText()
    assert '"relationship_inferred": false' in widget.details.toPlainText()
    widget.close()


def test_empty_session_history_smoke_is_read_only() -> None:
    application()
    widget = SessionHistoryWidget(path_opener=lambda path: True)
    report = widget.smoke_report()

    assert report["session_history_view_present"] is True
    assert report["session_history_read_only"] is True
    assert report["session_history_loaded"] is False
    assert report["session_repair_group_count"] == 0
    assert report["session_verified_repair_group_count"] == 0
    assert report["session_repair_relationships_inferred"] is False
    assert report["session_repair_relationships_explicit_only"] is True
    assert widget.open_result_button.isEnabled() is False
    assert widget.open_receipt_button.isEnabled() is False
    widget.close()
