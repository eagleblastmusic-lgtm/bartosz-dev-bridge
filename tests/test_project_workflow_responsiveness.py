"""Qt responsiveness and typed partial-workflow failures."""
import subprocess
from dataclasses import replace
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from bdb_gui.project_center import ProjectCenterWindow
from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, new_project_record
from bdb_vnext.project_workflow import ProjectWorkflow, ProjectWorkflowError, CommandResult
from bdb_vnext.project_memory import ProjectMemoryStore


def wait(app, predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def test_worker_keeps_qt_navigation_and_stop_available_and_rejects_duplicate(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = ProjectCenterWindow(runtime_root=tmp_path)
    window._projection_timer.stop()
    monkeypatch.setattr(window, "_is_read_only", lambda: False)
    release = threading.Event()
    started = threading.Event()
    calls, pulses, results = [], [], []
    def operation():
        calls.append("create")
        started.set()
        assert release.wait(5)
        return "created"
    timer = QTimer(window)
    timer.timeout.connect(lambda: pulses.append(1))
    timer.start(10)
    try:
        assert window._run_workflow("create", "Creating", operation, results.append)
        wait(app, started.is_set)
        assert not window._run_workflow("create", "Creating", operation, results.append)
        window._sidebar.setCurrentRow(1)
        assert window._pages.currentIndex() == 1
        assert window._run_workflow("stop", "STOP", lambda: "fenced", results.append)
        wait(app, lambda: "fenced" in results and len(pulses) >= 5)
        assert calls == ["create"] and "create" in window._operations
        release.set()
        wait(app, lambda: not window._operations)
        assert "created" in results and window._new_button.isEnabled()
    finally:
        release.set()
        wait(app, lambda: not window._operations)
        window.close()


def test_worker_timeout_is_visible_and_clears_busy(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = ProjectCenterWindow(runtime_root=tmp_path)
    window._projection_timer.stop()
    monkeypatch.setattr(window, "_is_read_only", lambda: False)
    def timeout(): raise subprocess.TimeoutExpired("git", 30)
    try:
        window._run_workflow("prompt", "Preparing", timeout, lambda result: pytest.fail("timeout cannot succeed"))
        wait(app, lambda: not window._operations)
        assert "command_timeout" in window._status.text()
        assert window._new_button.isEnabled()
    finally:
        window.close()


class TimeoutRunner:
    def run(self, args, **kwargs):
        raise subprocess.TimeoutExpired(args, 30)


def test_public_repo_reads_map_timeouts_and_preserve_checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    workflow = ProjectWorkflow(tmp_path / "runtime", command_runner=TimeoutRunner())
    brief = ProjectBrief("Existing", "goal", "description", "tool")
    with pytest.raises(ProjectWorkflowError) as caught:
        workflow.register_existing(display_name="Existing", repo_alias="existing", local_repo_path=repo, brief=brief)
    assert caught.value.code == "command_timeout" and (repo / ".git").exists()
    project = new_project_record(project_id="p1", display_name="Existing", repo_alias="existing", local_repo_path=repo, github_repo=None, brief=brief)
    with pytest.raises(ProjectWorkflowError) as caught: workflow.current_repo_head(project)
    assert caught.value.code == "command_timeout"


def test_partial_creation_returns_actual_path_and_registration_recovery(tmp_path):
    class Runner:
        def run(self, args, *, cwd, **kwargs):
            if args[1] == "init": (Path(cwd) / ".git").mkdir()
            if args[1] == "commit": raise subprocess.TimeoutExpired(args, 30)
            return CommandResult(tuple(args), 0, "", "")
    workflow = ProjectWorkflow(tmp_path / "runtime", command_runner=Runner())
    result = workflow.create_new(display_name="Partial", repo_alias="partial", projects_root=tmp_path / "projects", brief=ProjectBrief("Partial", "goal", "description", "tool"), github_name="partial")
    assert not result.ok and result.error_code == "command_timeout"
    assert result.recovery_action == "register_existing"
    assert Path(result.local_repo_path, ".git").exists()


def test_projection_timer_updates_visible_progress_with_only_background_reads(tmp_path, monkeypatch):
    from bdb_vnext.project_catalog import ProjectPlan, ProjectMilestone, ProjectTask
    app = QApplication.instance() or QApplication([])
    catalog = ProjectCatalog(tmp_path)
    project = new_project_record(project_id="p1", display_name="Projection", repo_alias="projection", local_repo_path=tmp_path / "repo", github_repo=None, brief=ProjectBrief("Projection", "goal", "description", "tool"))
    project = replace(project, plan_imported=True, plan_version="1", total_tasks=1, current_task="t1")
    catalog.upsert(project)
    store = ProjectMemoryStore(tmp_path, "p1")
    store.ensure_initial_plan(ProjectPlan("p1", "Projection", "1", (ProjectMilestone("m1", "One", "One", "active"),), (ProjectTask("t1", "m1", "One", "One", "pending"),), "t1"))
    window = ProjectCenterWindow(runtime_root=tmp_path, catalog=catalog)
    window._projection_timer.stop()
    monkeypatch.setattr(window, "_is_read_only", lambda: False)
    window._projects = (project,)
    window._select_project("p1")
    catalog.upsert(replace(project, completed_tasks=1, current_task=None))
    store.write_transaction(lambda state: (replace(state, execution={**state.execution, "task_statuses": {"t1": "completed"}}), None))
    reads = []
    original = ProjectMemoryStore.read_state
    def read_only(self):
        reads.append(threading.current_thread() is threading.main_thread())
        time.sleep(.02)
        return original(self)
    def no_writes(*args, **kwargs): pytest.fail("A projection timer must not write Memory")
    monkeypatch.setattr(ProjectMemoryStore, "read_state", read_only)
    monkeypatch.setattr(ProjectMemoryStore, "write_transaction", no_writes)
    window._bootstrap_ok = True
    try:
        window._poll_project_projection()
        wait(app, lambda: not window._operations)
        assert "1/1" in window._project_progress.text()
        assert reads and not any(reads)
        assert "Project Memory niedostępna" not in window._memory_tabs.widget(0).toPlainText()
    finally:
        window.close()
