from __future__ import annotations

import pytest

from bdb_gui.history import GuiEvent, HistoryCursor, HistoryFilters, HistorySnapshot


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.history_view import HistoryWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-history-view"])


def gui_event(sequence: int) -> GuiEvent:
    return GuiEvent(
        event_id=f"journal:alpha:{sequence}",
        sequence=sequence,
        event_type="COMMAND_STATE_CHANGED",
        occurred_at=f"2026-07-18T21:00:{sequence:02d}Z",
        source="bridge",
        severity="info",
        correlation_id=f"command-{sequence}",
        session_id="session-1",
        command_id=f"session-1:{sequence:06d}",
        payload={"state": "executing", "sequence": sequence},
    )


def snapshot(
    sequences: tuple[int, ...],
    *,
    after: int,
    next_after: int,
    has_more: bool,
) -> HistorySnapshot:
    return HistorySnapshot(
        workspace_root="C:/workspaces/alpha",
        project_alias="alpha",
        events=tuple(gui_event(sequence) for sequence in sequences),
        cursor=HistoryCursor(after, next_after, has_more),
        filters=HistoryFilters(None, None),
        operator_operation_id="history-op",
    )


def test_widget_starts_read_only_without_project() -> None:
    app = application()
    widget = HistoryWidget()
    app.processEvents()

    report = widget.smoke_report()
    assert report["history_view_present"] is True
    assert report["history_read_only"] is True
    assert report["history_pagination_present"] is True
    assert report["history_filters_present"] is True
    assert report["history_loaded"] is False
    assert widget.refresh_button.isEnabled() is False
    assert widget.load_more_button.isEnabled() is False
    widget.close()


def test_first_page_replaces_rows_and_enables_next_page() -> None:
    app = application()
    widget = HistoryWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot((1, 2), after=0, next_after=2, has_more=True), append=False)
    app.processEvents()

    assert widget.event_count == 2
    assert widget.table.rowCount() == 2
    assert widget.table.item(0, 0).text() == "1"
    assert widget.table.item(1, 3).text() == "COMMAND_STATE_CHANGED"
    assert widget.load_more_button.isEnabled() is True
    assert '"sequence": 2' in widget.details.toPlainText()
    assert widget.last_snapshot is not None
    assert widget.last_snapshot.mutation_operations_invoked == 0
    widget.close()


def test_next_page_appends_without_duplicate_sequences() -> None:
    app = application()
    widget = HistoryWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot((1, 2), after=0, next_after=2, has_more=True), append=False)
    widget.apply_snapshot(snapshot((2, 3), after=2, next_after=3, has_more=False), append=True)
    app.processEvents()

    assert widget.event_count == 3
    assert [widget.table.item(row, 0).text() for row in range(3)] == ["1", "2", "3"]
    assert widget.load_more_button.isEnabled() is False
    widget.close()


def test_refresh_and_load_more_emit_bounded_queries() -> None:
    app = application()
    widget = HistoryWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.session_filter.setText("session-1")
    widget.command_filter.setText("session-1:000001")
    widget.limit_spin.setValue(25)
    calls: list[dict[str, object]] = []
    widget.query_requested.connect(calls.append)

    widget.refresh_button.click()
    app.processEvents()
    assert calls == [
        {
            "after_event_id": 0,
            "limit": 25,
            "session_id": "session-1",
            "command_id": "session-1:000001",
            "append": False,
        }
    ]

    widget.apply_snapshot(snapshot((1,), after=0, next_after=1, has_more=True), append=False)
    widget.load_more_button.click()
    app.processEvents()
    assert calls[-1] == {
        "after_event_id": 1,
        "limit": 25,
        "session_id": "session-1",
        "command_id": "session-1:000001",
        "append": True,
    }
    widget.close()


def test_busy_state_blocks_queries() -> None:
    app = application()
    widget = HistoryWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    calls: list[dict[str, object]] = []
    widget.query_requested.connect(calls.append)
    widget.set_busy(True, "Odczyt w toku")

    widget.refresh_button.click()
    app.processEvents()

    assert calls == []
    assert widget.session_filter.isEnabled() is False
    assert widget.command_filter.isEnabled() is False
    widget.close()


def test_error_snapshot_is_visible_and_does_not_append() -> None:
    app = application()
    widget = HistoryWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot((1,), after=0, next_after=1, has_more=False), append=False)
    failure = HistorySnapshot.failure(
        "C:/workspaces/alpha",
        operation_id="failed",
        project_alias="alpha",
        error_code="journal_unavailable",
        error_message="Journal unavailable",
    )

    widget.apply_snapshot(failure, append=True)
    app.processEvents()

    assert widget.event_count == 1
    assert "journal_unavailable" in widget.feedback_label.text()
    assert widget.load_more_button.isEnabled() is False
    widget.close()
