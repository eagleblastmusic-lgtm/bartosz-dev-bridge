from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
import uuid

import pytest

from bdb_vnext.project_launch import ProjectLaunch
from bdb_vnext.project_launch_canonical import (
    ProjectLaunchCanonicalError,
    ProjectLaunchCanonicalState,
)


PROJECT_ID = "project-0001"
BINDING_ID = "binding-0001"
TASK_ID = "P3-02"
CONVERSATION_ID = "conversation-0001"


def rich_launch(**overrides) -> ProjectLaunch:
    values = {
        "launch_id": str(uuid.uuid4()),
        "repo_alias": "premium-calculator",
        "prompt": "Continue P3-02",
        "auto_send": False,
        "created_at": "2026-09-16T10:00:00.000000Z",
        "expires_at": "2026-09-16T10:10:00.000000Z",
        "project_id": PROJECT_ID,
        "plan_version": "1",
        "task_id": TASK_ID,
        "execution_binding_id": BINDING_ID,
        "correlation_id": "corr-0001",
        "command_id": "command-0001",
        "expected_repo_head_before": "7" * 40,
    }
    values.update(overrides)
    return ProjectLaunch(**values)


@dataclass(frozen=True)
class _State:
    execution: dict


class _Memory:
    def __init__(self, launch: ProjectLaunch, *, task_status: str = "blocked") -> None:
        self.state = _State(
            execution={
                "bindings": [
                    {
                        "project_id": launch.project_id,
                        "plan_version": launch.plan_version,
                        "task_id": launch.task_id,
                        "execution_binding_id": launch.execution_binding_id,
                        "launch_id": launch.launch_id,
                        "correlation_id": launch.correlation_id,
                        "command_id": launch.command_id,
                        "repo_alias": launch.repo_alias,
                        "expected_repo_head_before": launch.expected_repo_head_before,
                        "status": "ACTIVE",
                        "superseded": False,
                    }
                ],
                "launch_outbox": {
                    launch.launch_id: {
                        "prompt": launch.prompt,
                        "auto_send": launch.auto_send,
                        "project_id": launch.project_id,
                        "plan_version": launch.plan_version,
                        "task_id": launch.task_id,
                        "execution_binding_id": launch.execution_binding_id,
                        "correlation_id": launch.correlation_id,
                        "command_id": launch.command_id,
                        "repo_alias": launch.repo_alias,
                        "expected_repo_head_before": launch.expected_repo_head_before,
                        "status": "PUBLISHED",
                    }
                },
                "task_statuses": {TASK_ID: task_status},
                "current_task_id": TASK_ID,
                "current_binding_id": BINDING_ID,
            }
        )

    def execution_transaction(self, transition):
        self.state, result = transition(self.state)
        return result


class _Execution:
    def __init__(self, launch: ProjectLaunch, *, task_status: str = "blocked") -> None:
        self.launch = launch
        self.task_status = task_status
        self.sequence: list[str] = []
        self.binding_value = SimpleNamespace(
            project_id=launch.project_id,
            plan_version=launch.plan_version,
            task_id=launch.task_id,
            execution_binding_id=launch.execution_binding_id,
            launch_id=launch.launch_id,
            correlation_id=launch.correlation_id,
            command_id=launch.command_id,
            repo_alias=launch.repo_alias,
            expected_repo_head_before=launch.expected_repo_head_before,
            status="ACTIVE",
            superseded=False,
            conversation_id=None,
        )
        self.outbox_value = SimpleNamespace(
            prompt=launch.prompt,
            auto_send=launch.auto_send,
            project_id=launch.project_id,
            plan_version=launch.plan_version,
            task_id=launch.task_id,
            execution_binding_id=launch.execution_binding_id,
            launch_id=launch.launch_id,
            correlation_id=launch.correlation_id,
            command_id=launch.command_id,
            repo_alias=launch.repo_alias,
            expected_repo_head_before=launch.expected_repo_head_before,
            status="PUBLISHED",
        )

    def binding(self, project_id, binding_id):
        assert project_id == PROJECT_ID
        assert binding_id == BINDING_ID
        return self.binding_value

    def launch_outbox_record(self, project_id, launch_id):
        assert project_id == PROJECT_ID
        assert launch_id == self.launch.launch_id
        return self.outbox_value

    def snapshot(self, project_id):
        assert project_id == PROJECT_ID
        return {
            "current_binding_id": BINDING_ID,
            "current_task_id": TASK_ID,
            "task_statuses": {TASK_ID: self.task_status},
        }

    def bind_conversation(self, project_id, binding_id, conversation_id):
        assert project_id == PROJECT_ID
        assert binding_id == BINDING_ID
        self.sequence.append("bind")
        current = self.binding_value.conversation_id
        if current not in (None, conversation_id):
            raise AssertionError("conversation mismatch")
        self.binding_value.conversation_id = conversation_id
        return self.binding_value

    def mark_outbox_acknowledged(self, project_id, launch_id, *, conversation_id=None):
        assert project_id == PROJECT_ID
        assert launch_id == self.launch.launch_id
        assert conversation_id == CONVERSATION_ID
        self.sequence.append("ack")
        self.outbox_value.status = "ACKNOWLEDGED"
        return self.outbox_value


def canonical(launch: ProjectLaunch, *, task_status: str = "blocked"):
    execution = _Execution(launch, task_status=task_status)
    memory = _Memory(launch, task_status=task_status)
    state = ProjectLaunchCanonicalState(
        ".",
        execution=execution,
        memory_factory=lambda _project_id: memory,
    )
    return state, execution, memory


def test_partial_rich_metadata_fails_closed() -> None:
    launch = rich_launch(command_id=None)

    with pytest.raises(ProjectLaunchCanonicalError) as exc:
        ProjectLaunchCanonicalState.is_canonical_launch(launch)

    assert exc.value.code == "project_launch_metadata_incomplete"


def test_browser_claim_reactivates_exact_blocked_retry_binding() -> None:
    launch = rich_launch()
    state, _execution, memory = canonical(launch, task_status="blocked")

    state.activate_claimed(launch)

    assert memory.state.execution["task_statuses"][TASK_ID] == "active"
    assert memory.state.execution["current_task_id"] == TASK_ID
    assert memory.state.execution["current_binding_id"] == BINDING_ID
    assert len(memory.state.execution["bindings"]) == 1


def test_browser_claim_never_reopens_completed_task() -> None:
    launch = rich_launch()
    state, _execution, memory = canonical(launch, task_status="completed")

    with pytest.raises(ProjectLaunchCanonicalError) as exc:
        state.activate_claimed(launch)

    assert exc.value.code == "project_launch_task_terminal"
    assert memory.state.execution["task_statuses"][TASK_ID] == "completed"


def test_ack_binds_conversation_before_marking_outbox_acknowledged() -> None:
    launch = rich_launch()
    state, execution, _memory = canonical(launch, task_status="active")

    state.acknowledge_delivery(launch, CONVERSATION_ID)

    assert execution.sequence == ["bind", "ack"]
    assert execution.binding_value.conversation_id == CONVERSATION_ID
    assert execution.outbox_value.status == "ACKNOWLEDGED"
    assert state.is_acknowledged(launch) is True


def test_acknowledged_outbox_is_authoritative_even_after_task_projection_changes() -> None:
    launch = rich_launch()
    state, execution, _memory = canonical(launch, task_status="active")
    execution.outbox_value.status = "ACKNOWLEDGED"
    execution.task_status = "completed"

    assert state.is_acknowledged(launch) is True
