from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_vnext.project_execution import ProjectExecutionBinding
from bdb_vnext.project_workflow import CommandResult, ProjectWorkflow, ProjectWorkflowError
from bdb_vnext.resilient_project_workflow import ResilientProjectWorkflow


HEAD_OLD = "a" * 40
HEAD_NEW = "b" * 40


class _Memory:
    def __init__(self, plan, execution):
        self._plan = plan
        self._state = SimpleNamespace(execution=execution)

    def current_plan(self):
        return self._plan

    def read_state(self):
        return self._state


class _Execution:
    def __init__(self):
        self.active = None
        self.new_binding_calls = []
        self.snapshot_value = {"attempts": []}

    def current_task_binding(self, project_id, task_id):
        return self.active

    def new_binding(self, project_id, *, task_id=None, expected_repo_head_before="unknown", **_kwargs):
        self.new_binding_calls.append((project_id, task_id, expected_repo_head_before))
        return ProjectExecutionBinding(
            execution_binding_id="binding-retry-0001",
            project_id=project_id,
            plan_version="1",
            task_id=task_id,
            launch_id="launch-retry-0001",
            correlation_id="corr-retry-0001",
            command_id="command-retry-0001",
            repo_alias="premium-calculator",
            expected_repo_head_before=expected_repo_head_before,
            created_at="2026-09-15T00:00:00Z",
            generation=2,
        )

    def snapshot(self, _project_id):
        return self.snapshot_value


class _Catalog:
    def __init__(self, project):
        self.project = project

    def get(self, project_id):
        return self.project if project_id == self.project.project_id else None


class _RetryProbe(ResilientProjectWorkflow):
    def __init__(self, *, project, plan, execution_state):
        self.catalog = _Catalog(project)
        self.execution = _Execution()
        self._memory = _Memory(plan, execution_state)
        self.queued = None

    def memory(self, _project_id):
        return self._memory

    def current_repo_head(self, _project):
        return HEAD_NEW

    def _queue_execution_prompt(self, project_id, kind, *, binding_override=None):
        self.queued = (project_id, kind, binding_override)
        return binding_override


def _task(task_id="P3-02", milestone_id="P3", status="pending"):
    return SimpleNamespace(task_id=task_id, milestone_id=milestone_id, status=status)


def _retry_probe(*, cursor="P3-02", task_status="blocked", run_status="blocked"):
    project = SimpleNamespace(
        project_id="project-0001",
        repo_alias="premium-calculator",
        local_repo_path="C:/repo",
        github_repo="eagleblastmusic-lgtm/premium-calculator",
    )
    plan = SimpleNamespace(plan_version="1", tasks=[_task()])
    execution = {
        "active_milestone_run": {
            "milestone_run_id": "milestone-run-p3",
            "milestone_id": "P3",
            "status": run_status,
            "current_task_id": "P3-02",
        },
        "current_task_id": cursor,
        "task_statuses": {"P3-02": task_status},
    }
    return _RetryProbe(project=project, plan=plan, execution_state=execution)


def test_blocked_milestone_retry_uses_same_task_with_fresh_head():
    workflow = _retry_probe()

    binding = workflow.queue_continue_prompt("project-0001")

    assert binding.task_id == "P3-02"
    assert binding.expected_repo_head_before == HEAD_NEW
    assert binding.generation == 2
    assert workflow.execution.new_binding_calls == [("project-0001", "P3-02", HEAD_NEW)]
    assert workflow.queued[:2] == ("project-0001", "continue")


def test_stale_invalid_execution_cursor_yields_to_blocked_run_subject():
    workflow = _retry_probe(cursor="P9-DELETED")

    binding = workflow.queue_continue_prompt("project-0001")

    assert binding.task_id == "P3-02"


def test_conflicting_valid_execution_cursor_fails_closed():
    workflow = _retry_probe(cursor="P3-03")
    workflow._memory._plan.tasks.append(_task("P3-03"))

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow.queue_continue_prompt("project-0001")

    assert exc.value.code == "execution_recovery_ambiguous"


def test_review_state_is_not_reopened_as_retry():
    workflow = _retry_probe(task_status="review")

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow.queue_continue_prompt("project-0001")

    assert exc.value.code == "execution_review_required"


def test_existing_active_retry_binding_fails_closed():
    workflow = _retry_probe()
    workflow.execution.active = SimpleNamespace(execution_binding_id="binding-live")

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow.queue_continue_prompt("project-0001")

    assert exc.value.code == "execution_retry_binding_active"


class _GitRunner:
    def __init__(self, *, repo: Path, local_head=HEAD_OLD, remote_head=HEAD_NEW, dirty=False, ancestor=True, origin="https://github.com/eagleblastmusic-lgtm/premium-calculator.git"):
        self.repo = repo
        self.local_head = local_head
        self.remote_head = remote_head
        self.dirty = dirty
        self.ancestor = ancestor
        self.origin = origin
        self.calls = []

    def run(self, args, *, cwd=None, timeout_seconds=120.0):
        args = tuple(args)
        self.calls.append((args, Path(cwd) if cwd is not None else None, timeout_seconds))
        assert args[0] == "git"
        cmd = args[1:]
        if cmd[:2] == ("status", "--porcelain=v1"):
            return CommandResult(args, 0, "dirty.txt\n" if self.dirty else "", "")
        if cmd == ("rev-parse", "--abbrev-ref", "HEAD"):
            return CommandResult(args, 0, "main\n", "")
        if cmd == ("remote", "get-url", "origin"):
            return CommandResult(args, 0, self.origin + "\n", "")
        if cmd[:3] == ("fetch", "--no-tags", "origin"):
            return CommandResult(args, 0, "", "")
        if cmd == ("rev-parse", "origin/main^{commit}"):
            return CommandResult(args, 0, self.remote_head + "\n", "")
        if cmd == ("rev-parse", "HEAD^{commit}"):
            return CommandResult(args, 0, self.local_head + "\n", "")
        if cmd[:2] == ("merge-base", "--is-ancestor"):
            return CommandResult(args, 0 if self.ancestor else 1, "", "")
        if cmd[:2] == ("merge", "--ff-only"):
            self.local_head = cmd[2]
            return CommandResult(args, 0, "", "")
        raise AssertionError(f"unexpected git call: {cmd}")


class _AlignmentProbe(ResilientProjectWorkflow):
    def __init__(self, project, runner):
        self.catalog = _Catalog(project)
        self.runner = runner
        self.execution = _Execution()


def _alignment_probe(tmp_path, **runner_kwargs):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = SimpleNamespace(
        project_id="project-0001",
        repo_alias="premium-calculator",
        local_repo_path=str(repo),
        github_repo="eagleblastmusic-lgtm/premium-calculator",
    )
    runner = _GitRunner(repo=repo, **runner_kwargs)
    return _AlignmentProbe(project, runner), runner


def test_safe_alignment_fast_forwards_only_to_exact_accepted_head(tmp_path):
    workflow, runner = _alignment_probe(tmp_path)

    assert workflow._safe_fast_forward_to_accepted_head("project-0001", HEAD_NEW) == HEAD_NEW
    assert runner.local_head == HEAD_NEW
    assert any(call[0][1:3] == ("merge", "--ff-only") for call in runner.calls)


def test_safe_alignment_rejects_dirty_checkout(tmp_path):
    workflow, _runner = _alignment_probe(tmp_path, dirty=True)

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow._safe_fast_forward_to_accepted_head("project-0001", HEAD_NEW)

    assert exc.value.code == "repo_alignment_dirty"


def test_safe_alignment_rejects_divergence(tmp_path):
    workflow, _runner = _alignment_probe(tmp_path, ancestor=False)

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow._safe_fast_forward_to_accepted_head("project-0001", HEAD_NEW)

    assert exc.value.code == "repo_alignment_non_fast_forward"


def test_safe_alignment_rejects_wrong_origin(tmp_path):
    workflow, _runner = _alignment_probe(tmp_path, origin="https://github.com/other/repo.git")

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow._safe_fast_forward_to_accepted_head("project-0001", HEAD_NEW)

    assert exc.value.code == "repo_alignment_origin_mismatch"


def test_safe_alignment_rejects_remote_head_other_than_accepted(tmp_path):
    workflow, _runner = _alignment_probe(tmp_path, remote_head="c" * 40)

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow._safe_fast_forward_to_accepted_head("project-0001", HEAD_NEW)

    assert exc.value.code == "repo_alignment_remote_mismatch"


def test_auto_next_alignment_failure_suppresses_stale_next_binding(monkeypatch, tmp_path):
    workflow, _runner = _alignment_probe(tmp_path, dirty=True)
    workflow.execution.snapshot_value = {
        "attempts": [
            {"task_id": "P3-01", "result_status": "PASS", "head_after": HEAD_NEW}
        ]
    }
    called = {"base": False}

    def base_next(self, project_id, *, completed_task_id=None):
        called["base"] = True
        return "launch", "ready"

    monkeypatch.setattr(ProjectWorkflow, "_ensure_auto_next_launch", base_next)

    launch, status = workflow._ensure_auto_next_launch("project-0001", completed_task_id="P3-01")

    assert launch is None
    assert status == "repo_alignment_required:repo_alignment_dirty"
    assert called["base"] is False


def test_auto_next_aligns_before_inherited_next_binding(monkeypatch, tmp_path):
    workflow, runner = _alignment_probe(tmp_path)
    workflow.execution.snapshot_value = {
        "attempts": [
            {"task_id": "P3-01", "result_status": "PASS", "head_after": HEAD_NEW}
        ]
    }
    observed = {}

    def base_next(self, project_id, *, completed_task_id=None):
        observed["head"] = runner.local_head
        observed["task"] = completed_task_id
        return "launch", "ready"

    monkeypatch.setattr(ProjectWorkflow, "_ensure_auto_next_launch", base_next)

    assert workflow._ensure_auto_next_launch("project-0001", completed_task_id="P3-01") == ("launch", "ready")
    assert observed == {"head": HEAD_NEW, "task": "P3-01"}


def test_runtime_entrypoints_bind_resilient_workflow():
    gui_source = Path("bdb_gui/app.py").read_text(encoding="utf-8")
    native_source = Path("packaging/windows/vnext_native_host_entry.py").read_text(encoding="utf-8")

    assert "_project_center.ProjectWorkflow = ResilientProjectWorkflow" in gui_source
    assert "native_host.ProjectWorkflow = ResilientProjectWorkflow" in native_source
