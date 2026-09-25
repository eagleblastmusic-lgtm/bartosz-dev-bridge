from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import pytest

from bdb_vnext.project_catalog import (
    ProjectBrief,
    ProjectCatalog,
    ProjectPlan,
    ProjectTask,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_execution import (
    ProjectExecutionCoordinator,
    ProjectExecutionError,
    ProjectExecutionSubmission,
    task_requires_code_delivery,
    verify_authoritative_code_delivery,
)
from bdb_vnext.project_memory import ProjectMemoryStore


def _init_git_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "tester@example.com"], check=True, capture_output=True)
    (repo / "README.md").write_text("# Test Project\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial commit"], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def _commit_file(repo: Path, file_rel: str, content: str = "export const x = 1;\n", msg: str = "commit") -> str:
    target = repo / file_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", msg], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def _fixture(
    tmp_path: Path,
    *,
    project_id: str = "test-semantic-acceptance",
    tasks: list[dict[str, object]] | None = None,
) -> tuple[ProjectCatalog, ProjectExecutionCoordinator, str, Path, str]:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    head_init = _init_git_repo(repo)

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
    return catalog, coordinator, project_id, repo, head_init


def test_p3_03_incident_semantic_false_pass_rejected(tmp_path: Path) -> None:
    """Incident reproduction: code deliverable with head_before == head_after, NOT_RUN, no canonical_refs must FAIL."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_init,  # No commit!
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


def test_code_deliverable_with_real_commit_passes(tmp_path: Path) -> None:
    """Code deliverable with real commit advancing git head succeeds."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    head_after = _commit_file(repo, "src/premium/calculator.ts", "export function calc() { return 42; }\n")

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_after,
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


def test_adversarial_fake_commit_hash_rejected(tmp_path: Path) -> None:
    """Adversarial submission with a fabricated commit SHA not present in git fails closed."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    fake_commit = "deadbeef" * 5

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": fake_commit,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "attempt claims fake commit sha",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"
    assert coordinator.snapshot(project_id)["task_statuses"]["P3-03"] == "active"


def test_adversarial_fake_promoted_rejected(tmp_path: Path) -> None:
    """Adversarial submission claiming PROMOTED without git advance or authoritative control.db record fails closed."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_init,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "PROMOTED",
        "result_summary": "fabricated PROMOTED status without authoritative cutover",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_adversarial_fake_canonical_refs_rejected(tmp_path: Path) -> None:
    """Adversarial submission with self-declared candidate/validation IDs not in control.db fails closed."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_init,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "fabricated candidate refs",
        "canonical_refs": {"candidate_id": "cand-fake-123", "validation_id": "val-fake-123"},
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_adversarial_non_ancestor_commit_rejected(tmp_path: Path) -> None:
    """Adversarial commit that is not a descendant of head_before fails closed."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    # Create an orphan branch with a commit not connected to main
    subprocess.run(["git", "-C", str(repo), "checkout", "--orphan", "disconnected"], check=True, capture_output=True)
    orphan_head = _commit_file(repo, "src/premium/calculator.ts", "export const orphan = true;\n", "orphan commit")
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, capture_output=True)

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": orphan_head,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "disconnected non-ancestor commit",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_adversarial_empty_diff_commit_rejected(tmp_path: Path) -> None:
    """Adversarial commit that produces an empty diff fails closed."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "empty commit"], check=True, capture_output=True)
    empty_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": empty_head,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "empty diff commit",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_adversarial_non_code_diff_rejected(tmp_path: Path) -> None:
    """Commit modifying only non-code files when code deliverables were declared fails closed."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    doc_head = _commit_file(repo, "docs/notes.txt", "just notes, no code\n", "docs only")

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": doc_head,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "docs only diff",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_authoritative_candidate_store_in_control_db_passes(tmp_path: Path) -> None:
    """Candidate and validation verified authoritatively in control.db succeed without git advance."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    # Set up control.db with verified candidate and validation
    ctrl_dir = catalog.runtime_root / "control"
    ctrl_dir.mkdir(parents=True, exist_ok=True)
    db_path = ctrl_dir / "control.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m4b_candidate_effects (
            candidate_id TEXT PRIMARY KEY,
            task_id TEXT,
            state TEXT,
            observed_tree_digest TEXT,
            planned_tree_digest TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS p1_validation_runs (
            validation_id TEXT PRIMARY KEY,
            candidate_id TEXT,
            status TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO m4b_candidate_effects VALUES (?, ?, ?, ?, ?)",
        ("cand-real-001", "P3-03", "SEALED", "sha256:" + "c" * 64, "sha256:" + "c" * 64),
    )
    conn.execute(
        "INSERT INTO p1_validation_runs VALUES (?, ?, ?)",
        ("val-real-001", "cand-real-001", "PASS"),
    )
    conn.commit()
    conn.close()

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_init,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "verified candidate and validation in control.db",
        "canonical_refs": {
            "candidate_id": "cand-real-001",
            "validation_id": "val-real-001",
            "candidate_tree_digest": "sha256:" + "c" * 64,
        },
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "PASS"
    assert coordinator.snapshot(project_id)["task_statuses"]["P3-03"] == "completed"


def test_authoritative_promotion_in_control_db_passes(tmp_path: Path) -> None:
    """Promotion verified authoritatively in control.db succeeds without immediate git head change."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)

    ctrl_dir = catalog.runtime_root / "control"
    ctrl_dir.mkdir(parents=True, exist_ok=True)
    db_path = ctrl_dir / "control.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m7c_promotion_cutovers (
            flow_id TEXT PRIMARY KEY,
            state TEXT
        )"""
    )
    conn.execute("INSERT INTO m7c_promotion_cutovers VALUES (?, ?)", ("flow-001", "ACTIVE"))
    conn.commit()
    conn.close()

    result_payload = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_init,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "PROMOTED",
        "result_summary": "authoritative promotion verified in control.db",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "PASS"
    assert coordinator.snapshot(project_id)["task_statuses"]["P3-03"] == "completed"


def test_legitimate_no_code_deliverables_pass_without_commit(tmp_path: Path) -> None:
    """Non-code tasks (reports, checklists, manual reviews) pass without head changes."""
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
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path, tasks=non_code_tasks)

    # Task T-DOC has validation report deliverables -> no code required
    binding_doc = coordinator.start(project_id, expected_repo_head_before=head_init)
    payload_doc = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "T-DOC",
        "execution_binding_id": binding_doc.execution_binding_id,
        "correlation_id": binding_doc.correlation_id,
        "command_id": binding_doc.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_init,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "validation record compiled",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }
    attempt_doc = coordinator.record_result(project_id, payload_doc)
    assert attempt_doc.result_status == "PASS"

    # Task T-REVIEW has manual criteria and no deliverables -> no code required
    binding_rev = coordinator.start(project_id, task_id="T-REVIEW", expected_repo_head_before=head_init)
    payload_rev = {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "T-REVIEW",
        "execution_binding_id": binding_rev.execution_binding_id,
        "correlation_id": binding_rev.correlation_id,
        "command_id": binding_rev.command_id,
        "repo_alias": "test-project",
        "head_before": head_init,
        "head_after": head_init,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "manual visual inspection completed",
        "criteria": [{"criterion": "manual:visual review", "type": "MANUAL", "status": "PASS"}],
    }
    attempt_rev = coordinator.record_result(project_id, payload_rev)
    assert attempt_rev.result_status == "PASS"
    assert attempt_rev.failure_code is None


def test_task_requires_code_delivery_classification_matrix() -> None:
    """Verifies fail-safe classification matrix for task_requires_code_delivery."""
    # 1. Code deliverable with manual/review criteria must still require code delivery
    t1 = ProjectTask(
        task_id="t1",
        milestone_id="m1",
        title="Code with manual criteria",
        description="desc",
        status="active",
        dependencies=(),
        acceptance_criteria=("manual: review calculator logic", "review: check coverage"),
        deliverables=("src/premium/calculator.ts",),
    )
    assert task_requires_code_delivery(t1) is True

    # 2. Mixed deliverables (code file + audit report) requires code delivery
    t2 = ProjectTask(
        task_id="t2",
        milestone_id="m1",
        title="Mixed deliverables",
        description="desc",
        status="active",
        dependencies=(),
        acceptance_criteria=("test:deterministic",),
        deliverables=("src/premium/calculator.ts", "test report summary"),
    )
    assert task_requires_code_delivery(t2) is True

    # 3. Code extension without directory path requires code delivery
    t3 = ProjectTask(
        task_id="t3",
        milestone_id="m1",
        title="Code extension only",
        description="desc",
        status="active",
        dependencies=(),
        acceptance_criteria=("test:deterministic",),
        deliverables=("calculator.py",),
    )
    assert task_requires_code_delivery(t3) is True

    # 4. Code directory path requires code delivery
    t4 = ProjectTask(
        task_id="t4",
        milestone_id="m1",
        title="Source path component",
        description="desc",
        status="active",
        dependencies=(),
        acceptance_criteria=("test:deterministic",),
        deliverables=("src/components/button",),
    )
    assert task_requires_code_delivery(t4) is True

    # 5. Strictly non-code deliverables do NOT require code delivery
    t5 = ProjectTask(
        task_id="t5",
        milestone_id="m1",
        title="Pure audit report",
        description="desc",
        status="active",
        dependencies=(),
        acceptance_criteria=("test:deterministic",),
        deliverables=("validation record", "test report summary"),
    )
    assert task_requires_code_delivery(t5) is False

    # 6. No deliverables and manual criteria does NOT require code delivery
    t6 = ProjectTask(
        task_id="t6",
        milestone_id="m1",
        title="Pure manual review",
        description="desc",
        status="active",
        dependencies=(),
        acceptance_criteria=("manual: visual check",),
        deliverables=(),
    )
    assert task_requires_code_delivery(t6) is False

    # 7. No deliverables does NOT require code delivery in repository
    t7 = ProjectTask(
        task_id="t7",
        milestone_id="m1",
        title="Task without declared deliverables",
        description="desc",
        status="active",
        dependencies=(),
        acceptance_criteria=("test: deterministic test suite",),
        deliverables=(),
    )
    assert task_requires_code_delivery(t7) is False
