from __future__ import annotations

import time
from pathlib import Path

import pytest

from bdb_gui.history import GuiEvent, HistoryCursor, HistoryFilters, HistorySnapshot
from bdb_gui.state import BootstrapSnapshot, GuiProject


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.main_window import ControlCenterWindow  # noqa: E402


class UnusedBootstrapService:
    def load(self, root: str):  # pragma: no cover
        raise AssertionError("constructor must not call bootstrap")


class UnusedOperationsService:
    def read_status(self, root: str):  # pragma: no cover
        raise AssertionError("history flow must not read status")

    def execute(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("history flow must not mutate")


class FakeCurrentOperationService:
    def read(self, root: str):  # pragma: no cover
        raise AssertionError("history flow must not read current operation")


class FakeHistoryService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def read(
        self,
        workspace_root: str,
        *,
        after_event_id: int,
        limit: int,
        session_id: str | None,
        command_id: str | None,
    ) -> HistorySnapshot:
        self.calls.append(
            {
                "workspace_root": workspace_root,
                "after_event_id": after_event_id,
                "limit": limit,
                "session_id": session_id,
                "command_id": command_id,
            }
        )
        sequence = after_event_id + 1
        return HistorySnapshot(
            workspace_root=workspace_root,
            project_alias="alpha",
            events=(
                GuiEvent(
                    event_id=f"journal:alpha:{sequence}",
                    sequence=sequence,
                    event_type="COMMAND_STATE_CHANGED",
                    occurred_at="2026-07-18T21:00:00Z",
                    source="bridge",
                    severity="info",
                    correlation_id="command-1",
                    session_id=session_id,
                    command_id=command_id,
                    payload={"state": "executing"},
                ),
            ),
            cursor=HistoryCursor(after_event_id, sequence, sequence < 2),
            filters=HistoryFilters(session_id, command_id),
            operator_operation_id=f"history-{sequence}",
        )


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-history-window"])


def wait_until(predicate, timeout: float = 5.0) -> None:
    app = application()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def project_snapshot(tmp_path: Path) -> BootstrapSnapshot:
    return BootstrapSnapshot.success(
        workspaces_root=str(tmp_path),
        projects=(
            GuiProject(
                alias="alpha",
                workspace_root=str(tmp_path / "alpha"),
                source_repo=str(tmp_path / "source"),
                source_branch="main",
                configured_status="prepared",
            ),
        ),
        operator_schema="bdb-operator-response-v1",
        operator_transport="in_process",
        network_listener=False,
        journal_access="read_only",
    )


def make_window(tmp_path: Path, history: FakeHistoryService) -> ControlCenterWindow:
    window = ControlCenterWindow(
        bootstrap_service=UnusedBootstrapService(),  # type: ignore[arg-type]
        operations_service=UnusedOperationsService(),  # type: ignore[arg-type]
        current_operation_service=FakeCurrentOperationService(),  # type: ignore[arg-type]
        history_service=history,  # type: ignore[arg-type]
        workspaces_root=str(tmp_path),
        auto_load_status=False,
        confirmation_provider=lambda action, root: False,
    )
    window._apply_bootstrap_snapshot(project_snapshot(tmp_path))
    return window


def test_window_reads_first_and_next_bounded_history_pages(tmp_path: Path) -> None:
    history = FakeHistoryService()
    window = make_window(tmp_path, history)

    window._start_history_read(
        {
            "after_event_id": 0,
            "limit": 25,
            "session_id": "session-1",
            "command_id": None,
            "append": False,
        }
    )
    wait_until(lambda: window.history_view.event_count == 1)

    window._start_history_read(
        {
            "after_event_id": 1,
            "limit": 25,
            "session_id": "session-1",
            "command_id": None,
            "append": True,
        }
    )
    wait_until(lambda: window.history_view.event_count == 2)

    workspace = str(tmp_path / "alpha")
    assert history.calls == [
        {
            "workspace_root": workspace,
            "after_event_id": 0,
            "limit": 25,
            "session_id": "session-1",
            "command_id": None,
        },
        {
            "workspace_root": workspace,
            "after_event_id": 1,
            "limit": 25,
            "session_id": "session-1",
            "command_id": None,
        },
    ]
    assert window.last_history is not None
    assert window.last_history.read_only is True
    assert window.last_history.mutation_operations_invoked == 0
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    window.close()


def test_invalid_gui_query_is_ignored_before_worker(tmp_path: Path) -> None:
    history = FakeHistoryService()
    window = make_window(tmp_path, history)

    window._start_history_read({"after_event_id": "zero", "limit": 100})
    application().processEvents()

    assert history.calls == []
    assert window.last_history is None
    window.close()


def test_history_does_not_autoload_during_generic_smoke_flow(tmp_path: Path) -> None:
    history = FakeHistoryService()
    window = make_window(tmp_path, history)
    application().processEvents()

    report = window.smoke_report()
    assert history.calls == []
    assert report["history_view_present"] is True
    assert report["history_loaded"] is False
    assert report["history_event_count"] == 0
    assert report["mutation_operations_invoked"] == 0
    window.close()
