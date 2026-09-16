from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from bdb_vnext.project_execution import ProjectExecutionBinding
from bdb_vnext.project_launch import ProjectLaunchQueueError
from bdb_vnext.stalled_launch_recovery import StalledLaunchRecoveryError, recover_stalled_launch


PROJECT_ID = "project-0001"
BINDING_ID = "binding-0001"
LAUNCH_ID = "launch-0001"


@dataclass(frozen=True)
class _State:
    execution: dict
    events: tuple = ()


class _Memory:
    def __init__(self, binding: ProjectExecutionBinding):
        self._plan = SimpleNamespace(
            plan_version="1",
            tasks=[SimpleNamespace(task_id=binding.task_id, status="pending")],
        )
        self.state = _State(
            execution={
                "bindings": [binding.to_dict()],
                "task_statuses": {binding.task_id: "blocked"},
                "current_task_id": binding.task_id,
                "current_binding_id": binding.execution_binding_id,
            }
        )

    def current_plan(self):
        return self._plan

    def execution_transaction(self, transition):
        self.state, result = transition(self.state)
        return result

    def _append_event(self, state, event_type, human_summary, **kwargs):
        event = {"event_type": event_type, "human_summary": human_summary, **kwargs}
        return replace(state, events=state.events + (event,))


class _Queue:
    def __init__(self):
        self.pending = None

    def peek(self):
        return self.pending


class _Execution:
    def __init__(self, binding, outbox):
        self.binding = binding
        self.outbox = outbox
        self.handoff = None
        self.watchdog_state = {
            "state": "STALLED",
            "resume_available": True,
            "execution_binding_id": binding.execution_binding_id,
            "task_id": binding.task_id,
        }

    def watchdog(self, project_id):
        assert project_id == PROJECT_ID
        return dict(self.watchdog_state)

    def resume_binding(self, project_id, binding_id):
        assert project_id == PROJECT_ID
        assert binding_id == self.binding.execution_binding_id
        return self.binding

    def launch_handoff(self, project_id, binding_id):
        assert project_id == PROJECT_ID
        assert binding_id == self.binding.execution_binding_id
        return self.handoff

    def launch_outbox_record(self, project_id, launch_id):
        assert project_id == PROJECT_ID
        assert launch_id == self.binding.launch_id
        return self.outbox


class _Workflow:
    def __init__(self, *, outbox_status="PUBLISHED", conversation_id=None):
        self.binding = ProjectExecutionBinding(
            execution_binding_id=BINDING_ID,
            project_id=PROJECT_ID,
            plan_version="1",
            task_id="P3-02",
            launch_id=LAUNCH_ID,
            correlation_id="corr-0001",
            command_id="command-0001",
            repo_alias="premium-calculator",
            expected_repo_head_before="7" * 40,
            created_at="2026-09-15T10:18:49Z",
            status="ACTIVE",
            superseded=False,
            generation=2,
            conversation_id=conversation_id,
        )
        self.outbox = SimpleNamespace(
            project_id=PROJECT_ID,
            execution_binding_id=BINDING_ID,
            task_id="P3-02",
            launch_id=LAUNCH_ID,
            status=outbox_status,
        )
        self.execution = _Execution(self.binding, self.outbox)
        self.queue = _Queue()
        self._memory = _Memory(self.binding)
        self.publish_calls = []
        self.publish_error = None

    def memory(self, project_id):
        assert project_id == PROJECT_ID
        return self._memory

    def publish_outbox_launch(self, project_id, launch_id):
        assert project_id == PROJECT_ID
        assert launch_id == LAUNCH_ID
        self.publish_calls.append((project_id, launch_id))
        if self.publish_error is not None:
            raise self.publish_error
        self.queue.pending = SimpleNamespace(
            launch_id=LAUNCH_ID,
            execution_binding_id=BINDING_ID,
        )
        return self.queue.pending


def test_dry_run_is_read_only_and_preserves_same_binding_identity():
    workflow = _Workflow()

    result = recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, workflow=workflow)

    assert result.status == "READY"
    assert result.applied is False
    assert result.execution_binding_id == BINDING_ID
    assert result.launch_id == LAUNCH_ID
    assert result.generation == 2
    assert workflow.publish_calls == []
    assert workflow._memory.state.execution["task_statuses"]["P3-02"] == "blocked"


def test_apply_reactivates_task_and_republishes_exact_same_launch_without_new_generation():
    workflow = _Workflow()

    result = recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, apply=True, workflow=workflow)

    assert result.status == "RECOVERED"
    assert result.applied is True
    assert result.execution_binding_id == BINDING_ID
    assert result.launch_id == LAUNCH_ID
    assert result.generation == 2
    assert workflow.publish_calls == [(PROJECT_ID, LAUNCH_ID)]
    assert workflow.queue.pending.launch_id == LAUNCH_ID
    assert workflow.queue.pending.execution_binding_id == BINDING_ID
    assert workflow._memory.state.execution["task_statuses"]["P3-02"] == "active"
    assert workflow._memory.state.execution["current_task_id"] == "P3-02"
    assert workflow._memory.state.execution["current_binding_id"] == BINDING_ID
    assert len(workflow._memory.state.execution["bindings"]) == 1
    assert workflow._memory.state.events[-1]["event_type"] == "EXECUTION_REPLAYED"


def test_queue_failure_compensates_task_and_cursor_state():
    workflow = _Workflow()
    before = dict(workflow._memory.state.execution)
    before_statuses = dict(before["task_statuses"])
    workflow.publish_error = ProjectLaunchQueueError("queue_busy", "project launch queue is busy")

    with pytest.raises(StalledLaunchRecoveryError) as exc:
        recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, apply=True, workflow=workflow)

    assert exc.value.code == "queue_busy"
    assert workflow._memory.state.execution["task_statuses"] == before_statuses
    assert workflow._memory.state.execution["current_task_id"] == before["current_task_id"]
    assert workflow._memory.state.execution["current_binding_id"] == before["current_binding_id"]
    assert workflow._memory.state.events == ()


def test_recovery_refuses_acknowledged_launch():
    workflow = _Workflow(outbox_status="ACKNOWLEDGED")

    with pytest.raises(StalledLaunchRecoveryError) as exc:
        recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, apply=True, workflow=workflow)

    assert exc.value.code == "launch_already_acknowledged"
    assert workflow.publish_calls == []


def test_recovery_refuses_binding_already_bound_to_conversation():
    workflow = _Workflow(conversation_id="conversation-0001")

    with pytest.raises(StalledLaunchRecoveryError) as exc:
        recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, apply=True, workflow=workflow)

    assert exc.value.code == "launch_has_conversation"
    assert workflow.publish_calls == []


def test_recovery_refuses_completed_browser_handoff():
    workflow = _Workflow()
    workflow.execution.handoff = {"status": "SENT"}

    with pytest.raises(StalledLaunchRecoveryError) as exc:
        recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, apply=True, workflow=workflow)

    assert exc.value.code == "launch_handoff_already_sent"
    assert workflow.publish_calls == []


def test_recovery_requires_watchdog_to_confirm_same_stalled_binding():
    workflow = _Workflow()
    workflow.execution.watchdog_state["state"] = "ACTIVE"
    workflow.execution.watchdog_state["resume_available"] = False

    with pytest.raises(StalledLaunchRecoveryError) as exc:
        recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, apply=True, workflow=workflow)

    assert exc.value.code == "execution_not_stalled"


def test_recovery_refuses_to_replace_another_queue_launch():
    workflow = _Workflow()
    workflow.queue.pending = SimpleNamespace(
        launch_id="another-launch",
        execution_binding_id="another-binding",
    )

    with pytest.raises(StalledLaunchRecoveryError) as exc:
        recover_stalled_launch("unused", PROJECT_ID, BINDING_ID, apply=True, workflow=workflow)

    assert exc.value.code == "queue_pending"
    assert workflow.publish_calls == []
