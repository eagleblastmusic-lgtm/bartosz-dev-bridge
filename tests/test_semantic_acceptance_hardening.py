from __future__ import annotations

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
    ProjectExecutionSubmission,
)
from bdb_vnext.project_memory import ProjectMemoryStore

HEAD = "a" * 40
HEAD_AFTER = "b" * 40


def _fixture(
    tmp_path: Path,
    *,
    project_id: str = "test-semantic-acceptance",
    tasks: list[dict[str, object]] | None = None,
) -> tuple[ProjectCatalog, ProjectExecutionCoordinator, str]:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    brief = ProjectBrief("Test Project", "semantic acceptance tests", "fixture", "test")
    project = new_project_record(
        project_id=project_id,
        display_name="Test Project",
        repo_alias="test-project",
        local_repo_path=repo,
        github_repo=None,
        brief=brief,
    )
    catalog = ProjectCatalog(runtime)
    catalog.upsert(project)

    default_tasks = [
        {
            "id": "P3-03",
            "milestone_id": "m1",
            "title": "Premium Calculation Engine",
            "description": "implement engine",
            "status": "active",
            "dependencies": [],
            "acceptance_criteria": ["test:deterministic"],
            "deliverables": ["src/premium/calculator.ts", "src/types/calculator.ts"],
        }
    ]

    selected_tasks = tasks if tasks is not None else default_tasks
    current_task = selected_tasks[0]["id"] if selected_tasks else None

    plan_doc = {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Test Project",
        "plan_version": 1,
        "milestones": [{"id": "m1", "title": "Engine", "description": "core engine", "status": "active"}],
        "tasks": selected_tasks,
        "current_task_id": current_task,
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
                "current_milestone": plan.current_milestone.title if plan.current_milestone else None,
                "current_task": plan.current_task_id,
                "plan_path": str(memory.current_pointer),
                "project_status": "active",
            }
        )
    )
    coordinator = ProjectExecutionCoordinator(runtime, catalog=catalog)
    return catalog, coordinator, project_id


def test_p3_03_incident_semantic_false_pass_rejected(tmp_path: Path) -> None:
    """Incident reproduction: code deliverable with head_before == head_after, NOT_RUN, no canonical_refs must FAIL."""
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": HEAD,
        "head_after": HEAD,  # No commit!
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "attempt passed tests locally but produced no deliverable commit",
        "evidence_refs": ["evidence:local-run"],
        "criteria": [
            {"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "evidence:local-run"}
        ],
    }

    attempt = coordinator.record_result(project_id, result_payload)

    # Must be marked FAIL despite execution_status PASS
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"

    # Verify task remains active (not completed)
    snapshot = coordinator.snapshot(project_id)
    assert snapshot["task_statuses"]["P3-03"] != "completed"
    assert snapshot["task_statuses"]["P3-03"] == "active"

    # Verify binding transitioned to FAILED
    updated_binding = coordinator.binding(project_id, binding.execution_binding_id)
    assert updated_binding.status == "FAILED"


def test_code_deliverable_with_commit_passes(tmp_path: Path) -> None:
    """Code deliverable with head_after != head_before succeeds."""
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": HEAD,
        "head_after": HEAD_AFTER,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "committed deliverable code",
        "evidence_refs": ["evidence:local-run"],
        "criteria": [
            {"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "evidence:local-run"}
        ],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "PASS"
    assert attempt.failure_code is None

    snapshot = coordinator.snapshot(project_id)
    assert snapshot["task_statuses"]["P3-03"] == "completed"

    updated_binding = coordinator.binding(project_id, binding.execution_binding_id)
    assert updated_binding.status == "ACCEPTED"


def test_code_deliverable_with_promotion_passes(tmp_path: Path) -> None:
    """Code deliverable with promotion_status == PROMOTED succeeds even if head_after == head_before."""
    catalog, coordinator, project_id = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": HEAD,
        "head_after": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "PROMOTED",
        "result_summary": "promoted via pipeline",
        "evidence_refs": ["evidence:pipeline"],
        "criteria": [
            {"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "evidence:pipeline"}
        ],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "PASS"
    assert coordinator.snapshot(project_id)["task_statuses"]["P3-03"] == "completed"


def test_code_deliverable_with_candidate_tree_digest_passes(tmp_path: Path) -> None:
    """Code deliverable with candidate_tree_digest succeeds even without immediate repo head advance."""
    catalog, coordinator, project_id = _fixture(tmp_path, project_id="test-canonical-digest")
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": HEAD,
        "head_after": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "staged candidate bundle",
        "evidence_refs": ["evidence:bundle"],
        "canonical_refs": {"candidate_tree_digest": "sha256:" + "c" * 64},
        "criteria": [
            {"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "evidence:bundle"}
        ],
    }
    attempt = coordinator.record_result(project_id, payload)
    assert attempt.result_status == "PASS"
    assert coordinator.snapshot(project_id)["task_statuses"]["P3-03"] == "completed"


def test_code_deliverable_with_candidate_and_validation_id_passes(tmp_path: Path) -> None:
    """Code deliverable with candidate_id and validation_id succeeds even without immediate repo head advance."""
    catalog, coordinator, project_id = _fixture(tmp_path, project_id="test-canonical-ids")
    binding = coordinator.start(project_id, expected_repo_head_before=HEAD)
    payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": HEAD,
        "head_after": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "staged candidate and validated",
        "evidence_refs": ["evidence:val"],
        "canonical_refs": {"candidate_id": "cand-001", "validation_id": "val-001"},
        "criteria": [
            {"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "evidence:val"}
        ],
    }
    attempt = coordinator.record_result(project_id, payload)
    assert attempt.result_status == "PASS"
    assert coordinator.snapshot(project_id)["task_statuses"]["P3-03"] == "completed"


def test_legitimate_no_code_deliverables_pass_without_commit(tmp_path: Path) -> None:
    """Non-code tasks (reports, checklists, manual reviews) can pass without head changes."""
    non_code_tasks = [
        {
            "id": "T-DOC",
            "milestone_id": "m1",
            "title": "Validation Documentation",
            "description": "produce validation summary",
            "status": "active",
            "dependencies": [],
            "acceptance_criteria": ["test:deterministic"],
            "deliverables": ["validation record", "test report summary"],
        },
        {
            "id": "T-REVIEW",
            "milestone_id": "m1",
            "title": "Visual Review",
            "description": "perform visual check",
            "status": "pending",
            "dependencies": ["T-DOC"],
            "acceptance_criteria": ["manual:visual review"],
        },
    ]
    catalog, coordinator, project_id = _fixture(tmp_path, tasks=non_code_tasks)

    # Task T-DOC has validation report deliverables -> no code required
    binding_doc = coordinator.start(project_id, expected_repo_head_before=HEAD)
    payload_doc = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "T-DOC",
        "execution_binding_id": binding_doc.execution_binding_id,
        "correlation_id": binding_doc.correlation_id,
        "command_id": binding_doc.command_id,
        "repo_alias": "test-project",
        "head_before": HEAD,
        "head_after": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "validation record compiled",
        "evidence_refs": ["evidence:doc"],
        "criteria": [
            {"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS", "evidence_ref": "evidence:doc"}
        ],
    }
    attempt_doc = coordinator.record_result(project_id, payload_doc)
    assert attempt_doc.result_status == "PASS"

    # Task T-REVIEW has manual criteria and no deliverables -> no code required
    binding_rev = coordinator.start(project_id, expected_repo_head_before=HEAD)
    payload_rev = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "T-REVIEW",
        "execution_binding_id": binding_rev.execution_binding_id,
        "correlation_id": binding_rev.correlation_id,
        "command_id": binding_rev.command_id,
        "repo_alias": "test-project",
        "head_before": HEAD,
        "head_after": HEAD,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "manual visual inspection completed",
        "evidence_refs": ["evidence:review"],
        "criteria": [
            {"criterion": "manual:visual review", "type": "MANUAL", "status": "PASS", "evidence_ref": "evidence:review"}
        ],
    }
    attempt_rev = coordinator.record_result(project_id, payload_rev)
    # Manual criterion with PASS status passes without code delivery
    assert attempt_rev.result_status == "PASS"
    assert attempt_rev.failure_code is None
