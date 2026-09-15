from __future__ import annotations

from types import SimpleNamespace

import pytest

from bdb_vnext.project_execution import ProjectExecutionBinding
from bdb_vnext.project_workflow import ProjectWorkflowError
from bdb_vnext.resilient_project_workflow import ResilientProjectWorkflow


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

    def current_task_binding(self, project_id, task_id):
        return self.active

    def new_binding(self, project_id, *, task_id=None, expected_repo_head_before="unknown", **_kwargs):
        self.new_binding_calls.append((project_id, task_id, expected_repo_head_before))
        return ProjectExecutionBinding(
            execution_binding_id="binding-retry-0002",
            project_id=project_id,
            plan_version="1",
            task_id=task_id,
            launch_id="launch-retry-0002",
            correlation_id="corr-retry-0002",
            command_id="command-retry-0002",
            repo_alias="premium-calculator",
            expected_repo_head_before=expected_repo_head_before,
            created_at="2026-09-15T09:30:00Z",
            generation=2,
        )


class _Catalog:
    def __init__(self, project):
        self.project = project

    def get(self, project_id):
        return self.project if project_id == self.project.project_id else None


class _Probe(ResilientProjectWorkflow):
    def __init__(self, execution_state):
        self.project = SimpleNamespace(
            project_id="project-0001",
            repo_alias="premium-calculator",
            local_repo_path="C:/repo",
            github_repo="eagleblastmusic-lgtm/premium-calculator",
        )
        self.plan = SimpleNamespace(
            plan_version="1",
            tasks=[SimpleNamespace(task_id="P3-02", milestone_id="P3", status="pending")],
        )
        self.catalog = _Catalog(self.project)
        self.execution = _Execution()
        self._memory = _Memory(self.plan, execution_state)
        self.queued = None

    def memory(self, _project_id):
        return self._memory

    def current_repo_head(self, _project):
        return HEAD_NEW

    def _queue_execution_prompt(self, project_id, kind, *, binding_override=None):
        self.queued = (project_id, kind, binding_override)
        return binding_override


def _live_stale_projection():
    return {
        "current_task_id": None,
        "current_binding_id": None,
        "task_statuses": {"P3-02": "blocked"},
        "active_milestone_run": {
            "milestone_run_id": "milestone-run-p0",
            "milestone_id": "P0",
            "status": "completed",
            "current_task_id": None,
        },
        "attempts": [
            {
                "task_id": "P3-02",
                "plan_version": "1",
                "execution_status": "BLOCKED",
                "result_status": "FAIL",
                "failure_code": "HEAD_MISMATCH",
            }
        ],
    }


def test_terminal_failed_attempt_recovers_blocked_task_when_active_run_is_stale_completed():
    workflow = _Probe(_live_stale_projection())

    binding = workflow.queue_continue_prompt("project-0001")

    assert binding.task_id == "P3-02"
    assert binding.expected_repo_head_before == HEAD_NEW
    assert workflow.execution.new_binding_calls == [("project-0001", "P3-02", HEAD_NEW)]
    assert workflow.queued[:2] == ("project-0001", "continue")


def test_terminal_recovery_fails_closed_when_two_blocked_tasks_are_eligible():
    state = _live_stale_projection()
    state["task_statuses"]["P3-03"] = "blocked"
    state["attempts"].append(
        {
            "task_id": "P3-03",
            "plan_version": "1",
            "execution_status": "BLOCKED",
            "result_status": "FAIL",
        }
    )
    workflow = _Probe(state)
    workflow.plan.tasks.append(SimpleNamespace(task_id="P3-03", milestone_id="P3", status="pending"))

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow.queue_continue_prompt("project-0001")

    assert exc.value.code == "execution_recovery_ambiguous"


def test_terminal_recovery_refuses_to_override_running_stale_projection():
    state = _live_stale_projection()
    state["active_milestone_run"]["status"] = "running"
    workflow = _Probe(state)

    with pytest.raises(ProjectWorkflowError) as exc:
        workflow.queue_continue_prompt("project-0001")

    assert exc.value.code == "execution_recovery_ambiguous"
