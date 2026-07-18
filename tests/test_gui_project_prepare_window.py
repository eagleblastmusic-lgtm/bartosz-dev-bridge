from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.bootstrap import BootstrapService  # noqa: E402
from bdb_gui.project_window import ProjectControlCenterWindow  # noqa: E402
from bdb_gui.projects import PreparePlan, ProjectPrepareService  # noqa: E402
from bdb_gui.state import BootstrapSnapshot  # noqa: E402
from bdb_operator.models import OperatorResponse  # noqa: E402


class FakeBootstrapService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []

    def load(self, workspaces_root: str) -> BootstrapSnapshot:
        self.calls.append(workspaces_root)
        return BootstrapSnapshot.success(
            workspaces_root=str(self.root),
            projects=(),
            operator_schema="bdb-operator-response-v1",
            operator_transport="in_process",
            network_listener=False,
            journal_access="read_only",
        )


class UnusedOperationsService:
    def read_status(self, root: str):  # pragma: no cover
        raise AssertionError("prepare flow must not read status")

    def execute(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("prepare flow must not execute process controls")


class FakePrepareOperator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def prepare(self, workspace_root: str | Path, **kwargs: object) -> OperatorResponse:
        self.calls.append({"workspace_root": str(workspace_root), **kwargs})
        return OperatorResponse.success(
            "prepare",
            project_alias=str(kwargs["alias"]),
            operation_id="prepare-window-op",
            data={"status": "prepared", "workspace_root": str(workspace_root)},
        )


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-project-prepare-window"])


def wait_until(predicate, timeout: float = 5.0) -> None:
    app = application()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    return source


def initial_snapshot(root: Path) -> BootstrapSnapshot:
    return BootstrapSnapshot.success(
        workspaces_root=str(root),
        projects=(),
        operator_schema="bdb-operator-response-v1",
        operator_transport="in_process",
        network_listener=False,
        journal_access="read_only",
    )


def make_window(
    tmp_path: Path,
    operator: FakePrepareOperator,
    *,
    confirm: bool,
) -> tuple[ProjectControlCenterWindow, FakeBootstrapService]:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    bootstrap = FakeBootstrapService(workspaces)
    window = ProjectControlCenterWindow(
        bootstrap_service=bootstrap,  # type: ignore[arg-type]
        operations_service=UnusedOperationsService(),  # type: ignore[arg-type]
        project_prepare_service=ProjectPrepareService(operator),
        workspaces_root=str(workspaces),
        auto_load_status=False,
        confirmation_provider=lambda action, root: False,
        prepare_confirmation_provider=lambda plan: confirm,
    )
    window._apply_bootstrap_snapshot(initial_snapshot(workspaces))
    return window, bootstrap


def payload(tmp_path: Path) -> dict[str, object]:
    return {
        "alias": "alpha",
        "source_repo": str(make_source(tmp_path)),
        "allowed_paths": ["README.md", "tests/*.py"],
        "python_executable": sys.executable,
        "native_config": None,
        "test_timeout_seconds": 120,
        "max_patch_bytes": 262_144,
        "max_changed_files": 20,
        "auto_send_max_bytes": 24_000,
        "worker_timeout_seconds": 240,
    }


def test_generic_window_smoke_has_wizard_without_prepare(tmp_path: Path) -> None:
    operator = FakePrepareOperator()
    window, bootstrap = make_window(tmp_path, operator, confirm=False)
    application().processEvents()

    report = window.smoke_report()

    assert report["projects_wizard_present"] is True
    assert report["prepare_plan_required"] is True
    assert report["prepare_confirmation_required"] is True
    assert report["prepare_plan_loaded"] is False
    assert report["mutation_operations_invoked"] == 0
    assert operator.calls == []
    assert bootstrap.calls == []
    window.close()


def test_plan_worker_does_not_call_prepare_operator(tmp_path: Path) -> None:
    operator = FakePrepareOperator()
    window, _ = make_window(tmp_path, operator, confirm=False)

    window._start_prepare_plan(payload(tmp_path))
    wait_until(lambda: window.projects_view.plan is not None)

    assert operator.calls == []
    assert window.projects_view.plan is not None
    assert window.projects_view.plan.read_only is True
    assert window.projects_view.plan.mutation_operations_invoked == 0
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    window.close()


def test_cancelled_prepare_stops_before_operator_call(tmp_path: Path) -> None:
    operator = FakePrepareOperator()
    window, _ = make_window(tmp_path, operator, confirm=False)
    window._start_prepare_plan(payload(tmp_path))
    wait_until(lambda: window.projects_view.plan is not None)
    plan = window.projects_view.plan
    assert isinstance(plan, PreparePlan)

    window._request_prepare(plan)
    application().processEvents()

    assert operator.calls == []
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    assert "anulowany" in window.status_line.text()
    window.close()


def test_confirmed_prepare_executes_once_and_refreshes_catalog(tmp_path: Path) -> None:
    operator = FakePrepareOperator()
    window, bootstrap = make_window(tmp_path, operator, confirm=True)
    window._start_prepare_plan(payload(tmp_path))
    wait_until(lambda: window.projects_view.plan is not None)
    plan = window.projects_view.plan
    assert isinstance(plan, PreparePlan)

    window._request_prepare(plan)
    wait_until(lambda: len(operator.calls) == 1)
    wait_until(lambda: len(bootstrap.calls) == 1)

    assert operator.calls[0]["workspace_root"] == plan.workspace_root
    assert operator.calls[0]["source_repo"] == plan.source_repo
    assert operator.calls[0]["alias"] == "alpha"
    assert operator.calls[0]["allowed_paths"] == ("README.md", "tests/*.py")
    assert window.smoke_report()["mutation_operations_invoked"] == 1
    assert window.projects_view.plan_state.text() == "PROJEKT PRZYGOTOWANY"
    window.close()


def test_invalid_plan_never_reaches_operator(tmp_path: Path) -> None:
    operator = FakePrepareOperator()
    window, _ = make_window(tmp_path, operator, confirm=True)
    invalid = payload(tmp_path)
    invalid["alias"] = "../escape"

    window._start_prepare_plan(invalid)
    wait_until(lambda: "PLAN NIEPRAWIDŁOWY" == window.projects_view.plan_state.text())

    assert operator.calls == []
    assert window.projects_view.plan is None
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    window.close()
