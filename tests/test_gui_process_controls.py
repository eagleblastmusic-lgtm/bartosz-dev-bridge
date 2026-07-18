from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from bdb_gui.operations import ControlResult, ProjectStatusSnapshot
from bdb_gui.state import BootstrapSnapshot, GuiProject


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.main_window import ControlCenterWindow  # noqa: E402


class UnusedBootstrapService:
    def load(self, workspaces_root: str):  # pragma: no cover - constructor safety tripwire
        raise AssertionError("Window construction must not load bootstrap")


class FakeOperationsService:
    def __init__(self, *, block_control: bool = False) -> None:
        self.execute_calls: list[tuple[str, str, int]] = []
        self.status_calls: list[str] = []
        self.control_started = threading.Event()
        self.control_release = threading.Event()
        if not block_control:
            self.control_release.set()

    def execute(self, action: str, workspace_root: str, *, arm_minutes: int) -> ControlResult:
        self.execute_calls.append((action, workspace_root, arm_minutes))
        self.control_started.set()
        if not self.control_release.wait(timeout=5):
            raise TimeoutError("test control release timeout")
        return ControlResult(
            action=action,  # type: ignore[arg-type]
            workspace_root=workspace_root,
            project_alias="alpha",
            operator_operation_id=f"{action}-op",
            ok=True,
            operator_data={"status": "RUNNING" if action != "stop" else "OFFLINE"},
        )

    def read_status(self, workspace_root: str) -> ProjectStatusSnapshot:
        self.status_calls.append(workspace_root)
        return ProjectStatusSnapshot(
            workspace_root=workspace_root,
            project_alias="alpha",
            overall_status="RUNNING",
            bridge_status="RUNNING",
            bridge_pid=1101,
            bridge_pid_alive=True,
            native_armed=True,
            native_armed_until="2026-07-18T22:00:00Z",
            promoter_running=True,
            promoter_pid=1102,
            source_clean=True,
            source_head="b" * 40,
            operator_operation_id="status-op",
        )


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-bdb-control-center"])


def bootstrap_snapshot(tmp_path: Path) -> BootstrapSnapshot:
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
                allowed_paths=("README.md", "tests/*.py"),
            ),
        ),
        operator_schema="bdb-operator-response-v1",
        operator_transport="in_process",
        network_listener=False,
        journal_access="read_only",
    )


def make_window(
    tmp_path: Path,
    operations: FakeOperationsService,
    *,
    confirm: bool,
) -> ControlCenterWindow:
    window = ControlCenterWindow(
        bootstrap_service=UnusedBootstrapService(),  # type: ignore[arg-type]
        operations_service=operations,  # type: ignore[arg-type]
        workspaces_root=str(tmp_path),
        auto_load_status=False,
        confirmation_provider=lambda action, workspace: confirm,
    )
    window._apply_bootstrap_snapshot(bootstrap_snapshot(tmp_path))
    return window


def wait_until(predicate, timeout: float = 5.0) -> None:
    app = application()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_window_construction_and_bootstrap_snapshot_do_not_mutate(tmp_path: Path) -> None:
    app = application()
    operations = FakeOperationsService()
    window = make_window(tmp_path, operations, confirm=True)
    app.processEvents()

    report = window.smoke_report()

    assert report["read_only_startup"] is True
    assert report["mutation_operations_invoked"] == 0
    assert report["action_controls_present"] is True
    assert report["confirmation_required"] is True
    assert report["arm_minutes_min"] == 1
    assert report["arm_minutes_max"] == 60
    assert operations.execute_calls == []
    assert operations.status_calls == []
    window.close()


def test_cancelled_confirmation_never_reaches_operation_service(tmp_path: Path) -> None:
    operations = FakeOperationsService()
    window = make_window(tmp_path, operations, confirm=False)

    window._request_control("start")
    application().processEvents()

    assert operations.execute_calls == []
    assert operations.status_calls == []
    assert window.last_control_result is None
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    assert "anulowana" in window.status_line.text()
    window.close()


def test_confirmed_action_runs_once_then_reads_status(tmp_path: Path) -> None:
    operations = FakeOperationsService()
    window = make_window(tmp_path, operations, confirm=True)
    window.dashboard.arm_minutes_spin.setValue(42)

    window._request_control("rearm")
    wait_until(lambda: window.last_status is not None)

    workspace = str(tmp_path / "alpha")
    assert operations.execute_calls == [("rearm", workspace, 42)]
    assert operations.status_calls == [workspace]
    assert window.last_control_result is not None
    assert window.last_control_result.action == "rearm"
    assert window.last_control_result.mutation_operations_invoked == 1
    assert window.smoke_report()["mutation_operations_invoked"] == 1
    assert window.last_status is not None and window.last_status.read_only is True
    window.close()


def test_active_control_blocks_double_click_and_all_controls(tmp_path: Path) -> None:
    operations = FakeOperationsService(block_control=True)
    window = make_window(tmp_path, operations, confirm=True)

    window._request_control("start")
    wait_until(operations.control_started.is_set)

    assert window.dashboard.start_button.isEnabled() is False
    assert window.dashboard.stop_button.isEnabled() is False
    assert window.dashboard.rearm_button.isEnabled() is False
    assert window.dashboard.refresh_status_button.isEnabled() is False

    window._request_control("stop")
    application().processEvents()
    assert len(operations.execute_calls) == 1
    assert operations.execute_calls[0][0] == "start"

    operations.control_release.set()
    wait_until(lambda: window.last_status is not None)

    assert len(operations.execute_calls) == 1
    assert window.dashboard.start_button.isEnabled() is True
    assert window.dashboard.stop_button.isEnabled() is True
    assert window.dashboard.rearm_button.isEnabled() is True
    window.close()
