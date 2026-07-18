from __future__ import annotations

import time
from pathlib import Path

import pytest

from bdb_gui.current_operation import CurrentOperationSnapshot
from bdb_gui.operations import ProjectStatusSnapshot
from bdb_gui.state import BootstrapSnapshot, GuiProject


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.main_window import ControlCenterWindow  # noqa: E402


class UnusedBootstrapService:
    def load(self, workspaces_root: str):  # pragma: no cover
        raise AssertionError("constructor must not call bootstrap")


class FakeOperationsService:
    def __init__(self) -> None:
        self.status_calls: list[str] = []
        self.mutation_calls = 0

    def read_status(self, workspace_root: str) -> ProjectStatusSnapshot:
        self.status_calls.append(workspace_root)
        return ProjectStatusSnapshot(
            workspace_root=workspace_root,
            project_alias="alpha",
            overall_status="READY",
            bridge_status="RUNNING",
            bridge_pid=1001,
            bridge_pid_alive=True,
            native_armed=True,
            native_armed_until="2026-07-18T22:00:00Z",
            promoter_running=True,
            promoter_pid=1002,
            source_clean=True,
            source_head="a" * 40,
            operator_operation_id="status-op",
        )

    def execute(self, *args, **kwargs):  # pragma: no cover
        self.mutation_calls += 1
        raise AssertionError("P08 read flow must not mutate")


class FakeCurrentOperationService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read(self, workspace_root: str) -> CurrentOperationSnapshot:
        self.calls.append(workspace_root)
        return CurrentOperationSnapshot(
            workspace_root=workspace_root,
            project_alias="alpha",
            generated_at="2026-07-18T21:10:00Z",
            active=False,
            operation=None,
            operator_operation_id="current-op",
        )


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-current-operation-window"])


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
    workspace = tmp_path / "alpha"
    return BootstrapSnapshot.success(
        workspaces_root=str(tmp_path),
        projects=(
            GuiProject(
                alias="alpha",
                workspace_root=str(workspace),
                source_repo=str(tmp_path / "source-alpha"),
                source_branch="main",
                configured_status="prepared",
            ),
        ),
        operator_schema="bdb-operator-response-v1",
        operator_transport="in_process",
        network_listener=False,
        journal_access="read_only",
    )


def test_normal_project_flow_reads_status_then_current_operation(tmp_path: Path) -> None:
    operations = FakeOperationsService()
    current = FakeCurrentOperationService()
    window = ControlCenterWindow(
        bootstrap_service=UnusedBootstrapService(),  # type: ignore[arg-type]
        operations_service=operations,  # type: ignore[arg-type]
        current_operation_service=current,  # type: ignore[arg-type]
        workspaces_root=str(tmp_path),
        auto_load_status=True,
        confirmation_provider=lambda action, root: False,
    )

    window._apply_bootstrap_snapshot(project_snapshot(tmp_path))
    wait_until(lambda: window.last_current_operation is not None)

    workspace = str(tmp_path / "alpha")
    assert operations.status_calls == [workspace]
    assert current.calls == [workspace]
    assert operations.mutation_calls == 0
    assert window.last_status is not None
    assert window.last_status.mutation_operations_invoked == 0
    assert window.last_current_operation is not None
    assert window.last_current_operation.mutation_operations_invoked == 0
    assert window.current_operation_view.state_label.text() == "BRAK AKTYWNEJ OPERACJI"
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    window.close()


def test_headless_style_flow_does_not_read_status_or_journal(tmp_path: Path) -> None:
    operations = FakeOperationsService()
    current = FakeCurrentOperationService()
    window = ControlCenterWindow(
        bootstrap_service=UnusedBootstrapService(),  # type: ignore[arg-type]
        operations_service=operations,  # type: ignore[arg-type]
        current_operation_service=current,  # type: ignore[arg-type]
        workspaces_root=str(tmp_path),
        auto_load_status=False,
        confirmation_provider=lambda action, root: False,
    )

    window._apply_bootstrap_snapshot(project_snapshot(tmp_path))
    application().processEvents()

    assert operations.status_calls == []
    assert current.calls == []
    report = window.smoke_report()
    assert report["current_operation_view_present"] is True
    assert report["current_operation_loaded"] is False
    assert report["mutation_operations_invoked"] == 0
    window.close()
