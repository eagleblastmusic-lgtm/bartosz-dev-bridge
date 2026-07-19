from __future__ import annotations

import time
from pathlib import Path

import pytest

from bdb_gui.session_history import SessionHistoryService
from bdb_gui.state import BootstrapSnapshot, GuiProject
from bdb_operator import OperatorApi

from session_projection_fixture import workspace_fixture


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.session_history_window import SessionProjectControlCenterWindow  # noqa: E402


class UnusedBootstrapService:
    def load(self, root):  # pragma: no cover - failure guard
        raise AssertionError("bootstrap worker is not used in this test")


class UnusedOperationsService:
    def read_status(self, root):  # pragma: no cover - failure guard
        raise AssertionError("session history must not read process status")

    def execute(self, *args, **kwargs):  # pragma: no cover - failure guard
        raise AssertionError("session history must not execute process controls")


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-session-history-window"])


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application().processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_session_window_installs_two_history_tabs_and_serialized_read(tmp_path: Path) -> None:
    app = application()
    root, _, _, _ = workspace_fixture(tmp_path)
    window = SessionProjectControlCenterWindow(
        bootstrap_service=UnusedBootstrapService(),  # type: ignore[arg-type]
        operations_service=UnusedOperationsService(),  # type: ignore[arg-type]
        session_history_service=SessionHistoryService(
            OperatorApi(repo_root=tmp_path, platform_name="posix")
        ),
        workspaces_root=str(tmp_path / "workspaces"),
        auto_load_status=False,
        confirmation_provider=lambda action, workspace: False,
        prepare_confirmation_provider=lambda plan: False,
        session_path_opener=lambda path: True,
    )
    snapshot = BootstrapSnapshot.success(
        workspaces_root=str(tmp_path / "workspaces"),
        projects=(
            GuiProject(
                alias="sample",
                workspace_root=str(root),
                source_repo=str(tmp_path / "source"),
                source_branch="main",
                configured_status="prepared",
                allowed_paths=("src/app.py",),
            ),
        ),
        operator_schema="bdb-operator-response-v1",
        operator_transport="in_process",
        network_listener=False,
        journal_access="read_only",
    )
    window._apply_bootstrap_snapshot(snapshot)
    app.processEvents()

    initial = window.smoke_report()
    assert initial["history_tabs_present"] is True
    assert initial["session_history_view_present"] is True
    assert initial["session_history_loaded"] is False
    assert initial["mutation_operations_invoked"] == 0

    window._start_session_history_read(10)
    wait_until(lambda: window.last_session_history is not None)

    report = window.smoke_report()
    assert report["session_history_loaded"] is True
    assert report["session_history_count"] == 2
    assert report["session_repair_relationships_inferred"] is False
    assert report["mutation_operations_invoked"] == 0
    assert window._session_history_worker is None
    window.close()
