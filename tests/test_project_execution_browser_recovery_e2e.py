from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, new_project_record, validate_project_plan
from bdb_vnext.project_execution import ProjectExecutionBinding, ProjectExecutionCoordinator
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter
from bdb_vnext.project_memory import ProjectMemoryStore
from bdb_vnext.project_workflow import ProjectWorkflow, build_continue_prompt


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "0c62f1b8-2ce1-48d3-bae9-c3c32b9a84b6"
CONVERSATION_ID = "abcdef12-3456-4789-abcd-abcdef123456"
P3_BINDING_ID = "binding-6af2300d4b8a4675b7fc5c6be08ba372"
P3_LAUNCH_ID = "3c9c027d-0c6a-48df-8ae4-3aafea47b44d"
P3_CORRELATION_ID = "corr-6af2300d4b8a4675b7fc5c6be08ba372"
P3_COMMAND_ID = "command-6af2300d4b8a4675b7fc5c6be08ba372"
P3_ACCEPTANCE = (
    "Positive, zero, and negative totalInvestmentResult map to correct wording and non-color-only semantics.",
    "Zero inflation and deflation are explained correctly.",
    "The user can always understand when nominal APR and effective annual return differ.",
    "The real/nominal difference is never labeled profit.",
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _execution_result(project_id: str, binding, *, head_before: str, head_after: str, criteria: list[dict[str, str]]) -> dict[str, object]:
    return {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": str(binding.plan_version),
        "task_id": binding.task_id,
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": binding.repo_alias,
        "head_before": head_before,
        "head_after": head_after,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "deterministic Browser/Native recovery fixture",
        "evidence_refs": ["https://example.invalid/e2e"],
        "criteria": criteria,
    }


def test_collapsed_send_attempted_recovers_existing_result_through_canonical_native_and_advances_auto(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Browser/Native recovery contract")

    runtime = tmp_path / "runtime"
    repo = tmp_path / "premium-calculator"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "BDB E2E")
    _git(repo, "config", "user.email", "bdb-e2e@example.invalid")

    source_path = repo / "src" / "features" / "calculator" / "ResultsBreakdown.tsx"
    test_path = repo / "src" / "features" / "calculator" / "ResultsBreakdown.test.tsx"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("export const resultsBreakdown = 'before';\n", encoding="utf-8")
    test_path.write_text("test('old breakdown', () => {});\n", encoding="utf-8")
    _git(repo, "add", "src/features/calculator/ResultsBreakdown.tsx", "src/features/calculator/ResultsBreakdown.test.tsx")
    _git(repo, "commit", "-m", "baseline")
    head_before = _git(repo, "rev-parse", "HEAD")
    source_path.write_text("export const resultsBreakdown = 'after';\n", encoding="utf-8")
    test_path.write_text("test('new breakdown', () => {});\n", encoding="utf-8")
    _git(repo, "add", "src/features/calculator/ResultsBreakdown.tsx", "src/features/calculator/ResultsBreakdown.test.tsx")
    _git(repo, "commit", "-m", "deliver P3-03 code paths")
    head_after = _git(repo, "rev-parse", "HEAD")
    bare_origin = tmp_path / "premium-calculator-origin.git"
    origin_init = subprocess.run(["git", "init", "--bare", "--quiet", str(bare_origin)], capture_output=True, text=True, check=False)
    assert origin_init.returncode == 0, origin_init.stderr
    branch = _git(repo, "branch", "--show-current")
    _git(repo, "remote", "add", "origin", str(bare_origin))
    _git(repo, "push", "--quiet", "--set-upstream", "origin", branch)

    brief = ProjectBrief("Premium Calculator fixture", "exercise canonical AUTO recovery", "recovery test", "test")
    project = new_project_record(
        project_id=PROJECT_ID,
        display_name="Premium Calculator fixture",
        repo_alias="premium-calculator",
        local_repo_path=repo,
        github_repo=None,
        brief=brief,
    )
    catalog = ProjectCatalog(runtime)
    catalog.upsert(project)
    task_ids = [f"P3-{index:02d}" for index in range(1, 6)]
    tasks = []
    for index, task_id in enumerate(task_ids):
        acceptance = list(P3_ACCEPTANCE) if task_id == "P3-03" else ["test:fixture"]
        task = {
            "id": task_id,
            "milestone_id": "P3",
            "title": "P3-03 calculator delivery" if task_id == "P3-03" else f"Fixture task {task_id}",
            "description": "Deliver the calculator results breakdown." if task_id == "P3-03" else f"Deterministic fixture for {task_id}.",
            "status": "active" if index == 0 else "pending",
            "dependencies": [task_ids[index - 1]] if index else [],
            "acceptance_criteria": acceptance,
        }
        if task_id == "P3-03":
            task["deliverables"] = [
                "src/features/calculator/ResultsBreakdown.tsx",
                "src/features/calculator/ResultsBreakdown.test.tsx",
            ]
        tasks.append(task)
    plan = validate_project_plan({
        "schema": "bdb-project-plan-v1",
        "project_id": PROJECT_ID,
        "project_name": "Premium Calculator fixture",
        "plan_version": 1,
        "milestones": [{"id": "P3", "title": "P3", "description": "recovery fixture", "status": "active"}],
        "tasks": tasks,
        "current_task_id": "P3-01",
    }, expected_project_id=PROJECT_ID)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    memory.ensure_initial_plan(plan)
    project = replace(
        project,
        plan_imported=True,
        plan_version=plan.plan_version,
        total_tasks=len(plan.tasks),
        current_milestone="P3",
        current_task="P3-01",
        plan_path=str(memory.current_pointer),
        project_status="active",
    )
    catalog.upsert(project)

    coordinator = ProjectExecutionCoordinator(runtime, catalog=catalog)
    coordinator.begin_milestone_auto(PROJECT_ID, milestone_id="P3", milestone_run_id="milestone-run-71b90932793b4b9fa5c063007cccc39f")
    prior_criteria = [{"criterion": "test:fixture", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "https://example.invalid/e2e"}]
    for task_id in ("P3-01", "P3-02"):
        prior = coordinator.start(PROJECT_ID, task_id=task_id, expected_repo_head_before=head_before)
        coordinator.bind_conversation(PROJECT_ID, prior.execution_binding_id, CONVERSATION_ID)
        attempt = coordinator.record_result(
            PROJECT_ID,
            _execution_result(PROJECT_ID, prior, head_before=head_before, head_after=head_before, criteria=prior_criteria),
        )
        assert attempt.result_status == "PASS"

    prior_binding = coordinator.new_binding(
        PROJECT_ID,
        task_id="P3-03",
        expected_repo_head_before=head_before,
        launch_id=P3_LAUNCH_ID,
    )
    binding = replace(
        prior_binding,
        execution_binding_id=P3_BINDING_ID,
        correlation_id=P3_CORRELATION_ID,
        command_id=P3_COMMAND_ID,
        generation=4,
        conversation_id=CONVERSATION_ID,
    )
    prompt = build_continue_prompt(
        catalog.get(PROJECT_ID),
        plan=memory.current_plan(),
        state=memory.read_state(),
        git_head=head_before,
        binding=binding,
    )
    coordinator.prepare_launch(PROJECT_ID, binding=binding, prompt=prompt, auto_send=True)
    queue = ProjectLaunchQueueAdapter(runtime / "control" / "project-launch-queue.json")
    workflow = ProjectWorkflow(runtime, catalog=catalog, queue=queue)
    launch = workflow.publish_outbox_launch(PROJECT_ID, P3_LAUNCH_ID)

    before_browser = coordinator.snapshot(PROJECT_ID)
    assert launch.execution_binding_id == P3_BINDING_ID
    assert launch.plan_version == "1"
    assert coordinator.binding(PROJECT_ID, P3_BINDING_ID).generation == 4
    assert coordinator.binding(PROJECT_ID, P3_BINDING_ID).expected_repo_head_before == head_before
    assert before_browser["current_task_id"] == "P3-03"
    assert before_browser["task_statuses"]["P3-03"] == "active"
    assert before_browser["milestone_auto"]["completed_tasks"] == 2
    assert before_browser["milestone_auto"]["total_tasks"] == 5
    assert before_browser["launch_handoffs"][P3_BINDING_ID]["status"] == "PENDING"
    assert coordinator.launch_outbox_record(PROJECT_ID, P3_LAUNCH_ID).status == "PUBLISHED"
    assert not [item for item in before_browser["attempts"] if item["task_id"] == "P3-03"]

    result = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": PROJECT_ID,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": P3_BINDING_ID,
        "correlation_id": P3_CORRELATION_ID,
        "command_id": P3_COMMAND_ID,
        "repo_alias": "premium-calculator",
        "head_before": head_before,
        "head_after": head_after,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "PASS",
        "result_summary": "Canonical evidence reconciliation completed on the existing generation-4 binding.",
        "evidence_refs": [
            "https://github.com/eagleblastmusic-lgtm/premium-calculator/pull/23",
            "https://github.com/eagleblastmusic-lgtm/premium-calculator/commit/f9f5a7be56358ab04502ce5ac6fb1773b3fb6c7d",
            "https://github.com/eagleblastmusic-lgtm/premium-calculator/actions/runs/35780010984",
            "https://github.com/eagleblastmusic-lgtm/premium-calculator/commit/101344b2b0a84f50186de3d17a62b5d931381a69",
        ],
        "criteria": [
            {"criterion": criterion, "status": "PASS", "evidence_ref": "https://example.invalid/e2e"}
            for criterion in P3_ACCEPTANCE
        ],
        # Exact legacy response shape from the incident. The tuple is discarded
        # as untyped metadata by the Browser adapter and is never Native authority.
        "canonical_refs": [
            "attempt-ae7d9b6bb8b347b3b78402957d78fc3b",
            "sha256:a18b5ca5bc566524173c08d8cf14841ebfc4ea1c769fd58df39bcab826d3b138",
            "milestone-run-71b90932793b4b9fa5c063007cccc39f",
        ],
    }
    browser_args = [
        node,
        str(ROOT / "tests" / "fixtures" / "vnext_project_auto_recovery_native_e2e.cjs"),
        str(ROOT / "browser_extension_vnext" / "content_adapter.js"),
        str(ROOT / "browser_extension_vnext" / "transport_worker.js"),
        str(ROOT / "tests" / "fixtures" / "vnext_native_e2e_bridge.py"),
        str(runtime),
        sys.executable,
        str(ROOT),
    ]
    ambiguous = subprocess.run(
        browser_args,
        input=json.dumps({"conversation_id": CONVERSATION_ID, "launch": launch.to_dict(), "result": result, "dom_mode": "ambiguous-turn"}),
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert ambiguous.returncode == 0, ambiguous.stdout + ambiguous.stderr
    ambiguous_trace = json.loads(ambiguous.stdout)
    assert not [item for item in ambiguous_trace["nativeRequests"] if item["action"] == "project_launch_ack"]
    assert not [item for item in ambiguous_trace["nativeRequests"] if item["action"] == "project_execution_submit"]
    assert not ambiguous_trace["sends"]
    after_ambiguous = coordinator.snapshot(PROJECT_ID)
    assert after_ambiguous["task_statuses"]["P3-03"] == "active"
    assert after_ambiguous["launch_handoffs"][P3_BINDING_ID]["status"] == "PENDING"
    assert coordinator.launch_outbox_record(PROJECT_ID, P3_LAUNCH_ID).status == "PUBLISHED"
    assert not [item for item in after_ambiguous["attempts"] if item["task_id"] == "P3-03"]

    payload = {"conversation_id": CONVERSATION_ID, "launch": launch.to_dict(), "result": result}
    completed = subprocess.run(
        browser_args,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    trace = json.loads(completed.stdout)

    submissions = [
        item for item in trace["nativeRequests"]
        if item["action"] == "project_execution_submit"
        and item["request"].get("result", {}).get("execution_binding_id") == P3_BINDING_ID
    ]
    assert len(submissions) == 1
    assert submissions[0]["request"]["result"]["plan_version"] == "1"
    assert "canonical_refs" not in submissions[0]["request"]["result"]
    assert submissions[0]["response"]["receipt"]["accepted"] is True
    assert submissions[0]["response"]["receipt"]["task_status"] == "completed"
    assert submissions[0]["response"]["receipt"]["current_task_id"] == "P3-04"
    assert {item["action"] for item in trace["nativeRequests"]} <= {
        "project_execution_status", "project_launch_peek", "project_launch_claim",
        "project_launch_ack", "project_execution_submit",
    }, "recovery must use canonical Browser/Native operations without direct Project Memory mutation"
    p3_ack = [
        item for item in trace["nativeRequests"]
        if item["action"] == "project_launch_ack" and item["request"].get("launch_id") == P3_LAUNCH_ID
    ]
    assert len(p3_ack) == 1
    assert trace["nativeRequests"].index(p3_ack[0]) < trace["nativeRequests"].index(submissions[0])
    assert len(trace["sends"]) == 1
    assert "Task ID (copy exactly): P3-04" in trace["sends"][0]
    assert trace["sends"][0] != launch.prompt, "the existing P3-03 prompt must never be resent"

    after = coordinator.snapshot(PROJECT_ID)
    p3_attempts = [item for item in after["attempts"] if item["task_id"] == "P3-03"]
    assert len(p3_attempts) == 1
    assert p3_attempts[0]["execution_binding_id"] == P3_BINDING_ID
    assert p3_attempts[0]["result_status"] == "PASS"
    assert after["task_statuses"]["P3-03"] == "completed"
    assert after["current_task_id"] == "P3-04"
    assert after["milestone_auto"]["status"] == "RUNNABLE"
    assert after["milestone_auto"]["completed_tasks"] == 3
    assert after["milestone_auto"]["total_tasks"] == 5
    assert after["current_task_id"] == "P3-04"

    p3_binding = coordinator.binding(PROJECT_ID, P3_BINDING_ID)
    assert p3_binding.generation == 4
    assert p3_binding.expected_repo_head_before == head_before
    assert p3_binding.conversation_id == CONVERSATION_ID
    assert after["launch_handoffs"][P3_BINDING_ID]["status"] == "SENT"
    assert coordinator.launch_outbox_record(PROJECT_ID, P3_LAUNCH_ID).status == "ACKNOWLEDGED"

    p4_bindings = [item for item in after["bindings"] if item["task_id"] == "P3-04" and item["plan_version"] == "1"]
    assert len(p4_bindings) == 1
    assert len([item for item in after["bindings"] if item["task_id"] == "P3-03"]) == 1
    p4_binding_id = p4_bindings[0]["execution_binding_id"]
    p4_launch_id = p4_bindings[0]["launch_id"]
    p4_handoffs = [
        item for item in after["launch_handoffs"].values()
        if item["task_id"] == "P3-04" and item["execution_binding_id"] == p4_binding_id
    ]
    assert len(p4_handoffs) == 1
    assert p4_handoffs[0]["status"] == "SENT"
    assert p4_handoffs[0]["conversation_id"] == CONVERSATION_ID
    assert coordinator.launch_outbox_record(PROJECT_ID, p4_launch_id).status == "ACKNOWLEDGED"
    assert not [item for item in after["attempts"] if item["task_id"] == "P3-04"]
