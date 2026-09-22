"""Isolated Native/Project Memory lifecycle fixtures; no production state is used."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import M9B_NATIVE_REQUEST_SCHEMA, VNextNativeConfig, handle_message
from bdb_vnext.resilient_project_workflow import ResilientProjectWorkflow

from test_project_execution_integration import _fixture
from test_project_execution_submission import _result


CONVERSATION = "chatgpt-conversation-e2e"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _scenario(tmp_path: Path):
    catalog, coordinator, project_id = _fixture(tmp_path, all_deterministic=True)
    repo = Path(catalog.get(project_id).local_repo_path)
    _git(repo, "init", "-b", "main")
    _git(repo, "-c", "user.name=BDB Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "initial")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, text=True, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    workflow = ResilientProjectWorkflow(catalog.runtime_root, catalog=catalog)
    coordinator.begin_milestone_auto(project_id, milestone_id="m1", milestone_run_id="milestone-reliability-e2e")
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    return coordinator, workflow, config, project_id, repo


def _native(config: VNextNativeConfig, action: str, **fields):
    return handle_message(config, {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": str(uuid.uuid4()),
        "action": action,
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        **fields,
    })


def _deliver(config: VNextNativeConfig, launch) -> None:
    claim_id = str(uuid.uuid4())
    claimed = _native(config, "project_launch_claim", launch_id=launch.launch_id, claim_id=claim_id, conversation_id=CONVERSATION)
    assert claimed["status"] == "claimed"
    ack = _native(config, "project_launch_ack", launch_id=launch.launch_id, claim_id=claim_id,
                  conversation_id=CONVERSATION, handoff_status="SENT", project_id=launch.project_id,
                  execution_binding_id=launch.execution_binding_id)
    assert ack["status"] == "acknowledged"


def _submit(config: VNextNativeConfig, project_id: str, binding, head: str, **overrides):
    result = {**_result(project_id, binding, head_before=head, head_after=head), **overrides}
    return _native(config, "project_execution_submit", conversation_id=CONVERSATION,
                   launch_id=binding.launch_id, result=result)


def test_task_a_delivery_pass_acceptance_and_next_launch(tmp_path: Path) -> None:
    coordinator, workflow, config, project_id, repo = _scenario(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    launch = workflow.queue_continue_prompt(project_id)
    _deliver(config, launch)
    binding = coordinator.binding(project_id, launch.execution_binding_id)
    receipt = _submit(config, project_id, binding, head)["receipt"]
    assert receipt["accepted"] is True
    assert receipt["next_launch"]["task_id"] == "t2"
    assert coordinator.snapshot(project_id)["task_statuses"]["t1"] == "completed"
    assert coordinator.launch_outbox_record(project_id, launch.launch_id).status == "ACKNOWLEDGED"


def test_task_b_waiting_does_not_terminalize_then_pass_creates_next_launch(tmp_path: Path) -> None:
    coordinator, workflow, config, project_id, repo = _scenario(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    launch = workflow.queue_continue_prompt(project_id)
    _deliver(config, launch)
    binding = coordinator.binding(project_id, launch.execution_binding_id)
    with pytest.raises(Exception) as waiting:
        _submit(config, project_id, binding, head, execution_status="WAITING_EXTERNAL", validation_status="WAITING_EXTERNAL")
    assert getattr(waiting.value, "code", None) == "execution_result_non_terminal"
    assert coordinator.snapshot(project_id)["attempts"] == []
    assert coordinator.binding(project_id, binding.execution_binding_id).status == "ACTIVE"
    assert coordinator.snapshot(project_id)["task_statuses"].get("t1") != "blocked"
    receipt = _submit(config, project_id, binding, head)["receipt"]
    assert receipt["accepted"] is True
    assert receipt["next_launch"]["task_id"] == "t2"


def test_task_c_failed_generation_retries_at_new_head_then_passes(tmp_path: Path) -> None:
    coordinator, workflow, config, project_id, repo = _scenario(tmp_path)
    first_head = _git(repo, "rev-parse", "HEAD")
    first_launch = workflow.queue_continue_prompt(project_id)
    _deliver(config, first_launch)
    first = coordinator.binding(project_id, first_launch.execution_binding_id)
    failed = _submit(config, project_id, first, first_head, execution_status="FAIL", validation_status="FAIL", failure_code="VALIDATION_FAILED")["receipt"]
    assert failed["accepted"] is False
    assert coordinator.snapshot(project_id)["task_statuses"]["t1"] == "blocked"
    assert coordinator.binding(project_id, first.execution_binding_id).status == "FAILED"

    _git(repo, "-c", "user.name=BDB Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "retry candidate")
    _git(repo, "push", "origin", "main")
    retry_head = _git(repo, "rev-parse", "HEAD")
    retry_launch = workflow.queue_continue_prompt(project_id)
    retry = coordinator.binding(project_id, retry_launch.execution_binding_id)
    assert retry.generation == 2
    assert retry.execution_binding_id != first.execution_binding_id
    assert retry.command_id != first.command_id and retry.correlation_id != first.correlation_id
    assert retry.expected_repo_head_before == retry_head
    with pytest.raises(Exception) as stale:
        _submit(config, project_id, first, first_head, execution_status="PASS", validation_status="PASS")
    assert getattr(stale.value, "code", None) == "execution_binding_stale"
    _deliver(config, retry_launch)
    accepted = _submit(config, project_id, retry, retry_head)["receipt"]
    assert accepted["accepted"] is True
    assert accepted["next_launch"]["task_id"] == "t2"
    assert coordinator.snapshot(project_id)["task_statuses"]["t1"] == "completed"
