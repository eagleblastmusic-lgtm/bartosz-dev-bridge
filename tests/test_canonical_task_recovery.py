from __future__ import annotations

import json
from pathlib import Path
import pytest

from bdb_vnext.project_catalog import (
    ProjectBrief,
    ProjectCatalog,
    ProjectPlan,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_execution import (
    ProjectExecutionCoordinator,
    ProjectExecutionError,
)
from bdb_vnext.project_execution_recovery_cli import main as recovery_cli_main
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter
from bdb_vnext.project_memory import ProjectMemoryStore
from bdb_vnext.project_workflow import CommandResult, ProjectWorkflow

HEAD = "a" * 40
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
HEAD_3 = "3" * 40


def _incident_fixture(
    tmp_path: Path,
) -> tuple[ProjectCatalog, ProjectExecutionCoordinator, ProjectWorkflow, str, str]:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    project_id = "0c62f1b8-2ce1-48d3-bae9-c3c32b9a84b6"
    brief = ProjectBrief("Premium Calculator", "recovery incident fixture", "fixture", "test")
    project = new_project_record(
        project_id=project_id,
        display_name="Premium Calculator",
        repo_alias="premium-calculator",
        local_repo_path=repo,
        github_repo=None,
        brief=brief,
    )
    catalog = ProjectCatalog(runtime)
    catalog.upsert(project)

    plan_doc = {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Premium Calculator",
        "plan_version": 1,
        "milestones": [
            {"id": "m1", "title": "Milestone 3", "description": "phase 3 deliverables", "status": "active"}
        ],
        "tasks": [
            {
                "id": "P3-01",
                "milestone_id": "m1",
                "title": "Config Types",
                "description": "define config types",
                "status": "active",
                "dependencies": [],
                "acceptance_criteria": ["test:deterministic"],
                "deliverables": ["src/types/config.ts"],
            },
            {
                "id": "P3-02",
                "milestone_id": "m1",
                "title": "Rule Parser",
                "description": "implement parser",
                "status": "pending",
                "dependencies": ["P3-01"],
                "acceptance_criteria": ["test:deterministic"],
                "deliverables": ["src/parser/rules.ts"],
            },
            {
                "id": "P3-03",
                "milestone_id": "m1",
                "title": "Calculation Engine",
                "description": "implement engine",
                "status": "pending",
                "dependencies": ["P3-02"],
                "acceptance_criteria": ["test:deterministic"],
                "deliverables": ["src/premium/calculator.ts"],
            },
            {
                "id": "P3-04",
                "milestone_id": "m1",
                "title": "Integration Tests",
                "description": "implement tests",
                "status": "pending",
                "dependencies": ["P3-03"],
                "acceptance_criteria": ["test:deterministic"],
                "deliverables": ["tests/test_calculator.ts"],
            },
            {
                "id": "P3-05",
                "milestone_id": "m1",
                "title": "Release Package",
                "description": "package calculator",
                "status": "pending",
                "dependencies": ["P3-04"],
                "acceptance_criteria": ["test:deterministic"],
                "deliverables": ["dist/index.js"],
            },
        ],
        "current_task_id": "P3-01",
    }
    plan = validate_project_plan(plan_doc, expected_project_id=project_id)
    memory = ProjectMemoryStore(runtime, project_id)
    memory.ensure_initial_plan(plan)
    catalog.upsert(
        type(project)(
            **{
                **project.__dict__,
                "plan_imported": True,
                "plan_version": plan.plan_version,
                "total_tasks": len(plan.tasks),
                "current_milestone": "Milestone 3",
                "current_task": "P3-01",
                "plan_path": str(memory.current_pointer),
                "project_status": "active",
            }
        )
    )
    coordinator = ProjectExecutionCoordinator(runtime, catalog=catalog)

    class Runner:
        def run(self, args, *, cwd=None, timeout_seconds=120.0):
            return CommandResult(tuple(args), 0, HEAD_3 + "\n", "")

    queue = ProjectLaunchQueueAdapter(runtime / "control" / "project-launch-queue.json")
    workflow = ProjectWorkflow(runtime, catalog=catalog, command_runner=Runner(), queue=queue)

    # 1. Begin milestone auto
    coordinator.begin_milestone_auto(project_id, milestone_id="m1")

    # 2. Complete P3-01
    b1 = coordinator.start(project_id, expected_repo_head_before=HEAD)
    coordinator.record_result(
        project_id,
        {
            "schema": "bdb-project-execution-submission-v1",
            "project_id": project_id,
            "plan_version": "1",
            "task_id": "P3-01",
            "execution_binding_id": b1.execution_binding_id,
            "correlation_id": b1.correlation_id,
            "command_id": b1.command_id,
            "repo_alias": "premium-calculator",
            "head_before": HEAD,
            "head_after": HEAD_1,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "promotion_status": "NOT_RUN",
            "result_summary": "P3-01 completed",
            "evidence_refs": ["evidence:p301"],
            "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
        },
    )

    # 3. Complete P3-02
    b2 = coordinator.start(project_id, expected_repo_head_before=HEAD_1)
    coordinator.record_result(
        project_id,
        {
            "schema": "bdb-project-execution-submission-v1",
            "project_id": project_id,
            "plan_version": "1",
            "task_id": "P3-02",
            "execution_binding_id": b2.execution_binding_id,
            "correlation_id": b2.correlation_id,
            "command_id": b2.command_id,
            "repo_alias": "premium-calculator",
            "head_before": HEAD_1,
            "head_after": HEAD_2,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "promotion_status": "NOT_RUN",
            "result_summary": "P3-02 completed",
            "evidence_refs": ["evidence:p302"],
            "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
        },
    )

    # 4. Complete P3-03 (the incident attempt)
    b3 = coordinator.start(project_id, expected_repo_head_before=HEAD_2)
    att3 = coordinator.record_result(
        project_id,
        {
            "schema": "bdb-project-execution-submission-v1",
            "project_id": project_id,
            "plan_version": "1",
            "task_id": "P3-03",
            "execution_binding_id": b3.execution_binding_id,
            "correlation_id": b3.correlation_id,
            "command_id": b3.command_id,
            "repo_alias": "premium-calculator",
            "head_before": HEAD_2,
            "head_after": HEAD_3,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "promotion_status": "NOT_RUN",
            "result_summary": "P3-03 false acceptance attempt",
            "evidence_refs": ["evidence:p303"],
            "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
        },
    )
    p3_03_attempt_id = att3.attempt_id

    # 5. Start P3-04 (downstream active binding)
    b4 = coordinator.start(project_id, expected_repo_head_before=HEAD_3)
    coordinator.bind_conversation(project_id, b4.execution_binding_id, "conversation-downstream-p304")
    coordinator.mark_launch_handoff_sent(
        project_id,
        execution_binding_id=b4.execution_binding_id,
        launch_id=b4.launch_id,
        conversation_id="conversation-downstream-p304",
    )

    return catalog, coordinator, workflow, project_id, p3_03_attempt_id


def test_invalidate_task_completion_recovers_state(tmp_path: Path) -> None:
    catalog, coordinator, workflow, project_id, p3_03_attempt_id = _incident_fixture(tmp_path)

    # Pre-invalidation assertions
    pre_snap = coordinator.snapshot(project_id)
    assert pre_snap["task_statuses"]["P3-03"] == "completed"
    assert pre_snap["task_statuses"]["P3-04"] == "active"
    assert pre_snap["current_task_id"] == "P3-04"
    assert pre_snap["milestone_auto"]["completed_tasks"] == 3

    # Invalidate P3-03
    receipt = coordinator.invalidate_task_completion(
        project_id,
        "P3-03",
        reason="Incident recovery: invalid completion false PASS",
        invalidated_by="operator-audit",
    )

    # 1. Receipt validation
    assert receipt["schema"] == "bdb-completion-invalidation-receipt-v1"
    assert receipt["status"] in {"INVALIDATED", "invalidation_applied"}
    assert receipt["invalidated_task_id"] == "P3-03"
    assert receipt["invalidated_attempt_id"] == p3_03_attempt_id
    assert len(receipt["superseded_downstream_bindings"]) == 1

    # 2. Append-only history invariant: historical attempts NOT mutated
    post_snap = coordinator.snapshot(project_id)
    p3_03_attempt = next(a for a in post_snap["attempts"] if a["attempt_id"] == p3_03_attempt_id)
    assert p3_03_attempt["result_status"] == "PASS"  # Raw attempt remains untouched
    assert p3_03_attempt["task_id"] == "P3-03"

    # 3. Audit events and invalidation tracking
    memory = ProjectMemoryStore(catalog.runtime_root, project_id)
    state = memory.read_state()
    assert any(e.event_type == "TASK_COMPLETION_INVALIDATED" for e in state.events)
    assert len(state.execution.get("completion_invalidations", [])) == 1
    inv_record = state.execution["completion_invalidations"][0]
    assert inv_record["task_id"] == "P3-03"
    assert inv_record["attempt_id"] == p3_03_attempt_id
    assert inv_record["reason"] == "Incident recovery: invalid completion false PASS"

    # 4. Task statuses and milestone progress
    assert post_snap["task_statuses"]["P3-03"] == "active"
    assert post_snap["task_statuses"]["P3-04"] == "pending"
    assert post_snap["current_task_id"] == "P3-03"
    assert post_snap["current_binding_id"] is None
    assert post_snap["milestone_auto"]["completed_tasks"] == 2
    assert post_snap["milestone_auto"]["total_tasks"] == 5

    # 5. Downstream binding supersession and record preservation
    p3_04_binding = coordinator.binding(project_id, receipt["superseded_downstream_bindings"][0])
    assert p3_04_binding.status == "SUPERSEDED"
    assert p3_04_binding.superseded is True
    # Handoff record preserved
    assert coordinator.launch_handoff(project_id, p3_04_binding.execution_binding_id) is not None

    # 6. Invariants check
    ok, violations = coordinator.check_invariants(project_id)
    assert ok is True, f"Invariant violations: {violations}"
    assert violations == []


def test_post_invalidation_workflow_launch_reissues_p3_03(tmp_path: Path) -> None:
    catalog, coordinator, workflow, project_id, p3_03_attempt_id = _incident_fixture(tmp_path)

    # Invalidate P3-03
    workflow.invalidate_task_completion(
        project_id,
        "P3-03",
        reason="Incident recovery: invalid completion false PASS",
    )

    # Call ensure_auto_current_launch to prepare launch for P3-03
    launch, status = workflow.ensure_auto_current_launch(project_id)
    assert status in {"ready", "rearmed"}
    assert launch is not None
    assert launch.task_id == "P3-03"

    # Verify new binding is active for P3-03
    snap = coordinator.snapshot(project_id)
    assert snap["current_task_id"] == "P3-03"
    assert snap["current_binding_id"] == launch.execution_binding_id
    new_binding = coordinator.binding(project_id, launch.execution_binding_id)
    assert new_binding.task_id == "P3-03"
    assert new_binding.status == "ACTIVE"
    assert not new_binding.superseded


def test_invalidation_idempotency(tmp_path: Path) -> None:
    catalog, coordinator, workflow, project_id, p3_03_attempt_id = _incident_fixture(tmp_path)

    # First invalidation applies
    receipt1 = coordinator.invalidate_task_completion(project_id, "P3-03", reason="Audit 1")
    assert receipt1["status"] in {"INVALIDATED", "invalidation_applied"}

    events_count_before = len(ProjectMemoryStore(catalog.runtime_root, project_id).read_state().events)

    # Repeat call with same task returns noop_already_invalidated
    receipt2 = coordinator.invalidate_task_completion(project_id, "P3-03", reason="Audit 2")
    assert receipt2["status"] in {"ALREADY_INVALIDATED", "noop_already_invalidated"}
    assert receipt2["invalidated_task_id"] == "P3-03"

    events_count_after = len(ProjectMemoryStore(catalog.runtime_root, project_id).read_state().events)
    assert events_count_after == events_count_before


def test_invalidation_optimistic_failures(tmp_path: Path) -> None:
    catalog, coordinator, workflow, project_id, p3_03_attempt_id = _incident_fixture(tmp_path)

    # 1. Non-completed task
    with pytest.raises(ProjectExecutionError) as exc_info:
        coordinator.invalidate_task_completion(project_id, "P3-05", reason="Invalidate pending task")
    assert exc_info.value.code == "task_not_completed"

    # 2. Wrong attempt_id
    with pytest.raises(ProjectExecutionError) as exc_info:
        coordinator.invalidate_task_completion(
            project_id,
            "P3-03",
            attempt_id="attempt-nonexistent-12345",
            reason="Wrong attempt ID",
        )
    assert exc_info.value.code == "attempt_id_mismatch"

    # 3. Wrong expected_result_digest
    with pytest.raises(ProjectExecutionError) as exc_info:
        coordinator.invalidate_task_completion(
            project_id,
            "P3-03",
            expected_result_digest="sha256:" + "f" * 64,
            reason="Wrong digest",
        )
    assert exc_info.value.code == "result_digest_mismatch"


def test_recovery_cli_preview_and_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    catalog, coordinator, workflow, project_id, p3_03_attempt_id = _incident_fixture(tmp_path)

    # 1. Preview mode (no --apply)
    exit_code = recovery_cli_main([
        "--project-id", project_id,
        "--task-id", "P3-03",
        "--runtime-root", str(catalog.runtime_root),
        "--reason", "CLI audit test",
    ])
    assert exit_code == 0
    preview_out = json.loads(capsys.readouterr().out)
    assert preview_out["status"] == "PREVIEW"
    assert preview_out["task_id"] == "P3-03"
    assert preview_out["projected_task_status"] == "active"

    # 2. Apply without --yes fails closed
    exit_code = recovery_cli_main([
        "--project-id", project_id,
        "--task-id", "P3-03",
        "--runtime-root", str(catalog.runtime_root),
        "--reason", "CLI audit test",
        "--apply",
    ])
    assert exit_code == 2
    err_out = json.loads(capsys.readouterr().out)
    assert err_out["status"] == "FAILED"
    assert err_out["error_code"] == "explicit_confirmation_required"

    # 3. Apply with --yes succeeds
    exit_code = recovery_cli_main([
        "--project-id", project_id,
        "--task-id", "P3-03",
        "--runtime-root", str(catalog.runtime_root),
        "--reason", "CLI audit test",
        "--apply",
        "--yes",
    ])
    assert exit_code == 0
    applied_out = json.loads(capsys.readouterr().out)
    assert applied_out["status"] == "APPLIED"
    assert applied_out["invalidated_task_id"] == "P3-03"
