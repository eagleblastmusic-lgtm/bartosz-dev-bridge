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


def _write_candidate_control_db(
    runtime_root: Path,
    *,
    candidates: list[tuple[str, str | None, str, str | None, str | None]],
    validations: list[tuple[str, str | None, str]],
) -> None:
    control_dir = runtime_root / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(control_dir / "control.db")
    conn.executescript(
        """
        CREATE TABLE m4b_candidate_effects (
            candidate_id TEXT PRIMARY KEY,
            task_id TEXT,
            state TEXT,
            observed_tree_digest TEXT,
            planned_tree_digest TEXT
        );
        CREATE TABLE p1_validation_runs (
            validation_id TEXT PRIMARY KEY,
            candidate_id TEXT,
            status TEXT
        );
        """
    )
    conn.executemany("INSERT INTO m4b_candidate_effects VALUES (?, ?, ?, ?, ?)", candidates)
    conn.executemany("INSERT INTO p1_validation_runs VALUES (?, ?, ?)", validations)
    conn.commit()
    conn.close()


def _candidate_result_payload(project_id: str, binding: object, head: str, refs: dict[str, str]) -> dict[str, object]:
    return {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head,
        "head_after": head,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "NOT_RUN",
        "result_summary": "candidate lineage verification",
        "canonical_refs": refs,
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }


def _promotion_result_payload(project_id: str, binding: object, head: str, summary: str) -> dict[str, object]:
    return {
        "schema": "bdb-project-execution-submission-v1",
        "project_id": project_id,
        "plan_version": "1",
        "task_id": "P3-03",
        "execution_binding_id": binding.execution_binding_id,
        "correlation_id": binding.correlation_id,
        "command_id": binding.command_id,
        "repo_alias": "test-project",
        "head_before": head,
        "head_after": head,
        "execution_status": "PASS",
        "validation_status": "PASS",
        "promotion_status": "PROMOTED",
        "result_summary": summary,
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }


def _write_task_bound_promotion(runtime_root: Path, *, task_id: str) -> None:
    control_dir = runtime_root / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(control_dir / "control.db")
    conn.executescript(
        """
        CREATE TABLE m7c_promotion_cutovers (
            flow_id TEXT PRIMARY KEY,
            flow_revision_id TEXT NOT NULL,
            state TEXT NOT NULL
        );
        CREATE TABLE m7c_promotion_bindings (
            effect_id TEXT PRIMARY KEY,
            flow_id TEXT NOT NULL,
            flow_revision_id TEXT NOT NULL
        );
        CREATE TABLE m7a_git_effects (
            effect_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            work_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            state TEXT NOT NULL,
            effect_certainty TEXT NOT NULL,
            observed_ref_oid TEXT,
            prepared_commit_oid TEXT NOT NULL
        );
        CREATE TABLE m4b_candidate_effects (
            candidate_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            work_id TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO m7c_promotion_cutovers VALUES (?, ?, ?)", ("flow-001", "revision-001", "ACTIVE"))
    conn.execute("INSERT INTO m7c_promotion_bindings VALUES (?, ?, ?)", ("effect-001", "flow-001", "revision-001"))
    conn.execute(
        "INSERT INTO m7a_git_effects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("effect-001", task_id, "work-001", "candidate-001", "AFTER", "AFTER", "a" * 40, "a" * 40),
    )
    conn.execute("INSERT INTO m4b_candidate_effects VALUES (?, ?, ?)", ("candidate-001", task_id, "work-001"))
    conn.commit()
    conn.close()


def _write_n4_publication_control_db(
    runtime_root: Path,
    *,
    candidate_id: str | None,
    candidate_view_id: str | None,
    candidate_task_id: str | None = None,
    candidate_state: str = "SEALED",
) -> None:
    control_dir = runtime_root / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(control_dir / "control.db")
    conn.executescript(
        """
        CREATE TABLE m4b_candidate_effects (
            candidate_id TEXT PRIMARY KEY,
            task_id TEXT,
            state TEXT,
            observed_tree_digest TEXT,
            planned_tree_digest TEXT,
            work_id TEXT
        );
        CREATE TABLE n4_publications (
            publication_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            candidate_id TEXT,
            candidate_view_id TEXT
        );
        """
    )
    if candidate_id is not None:
        conn.execute(
            "INSERT INTO m4b_candidate_effects VALUES (?, ?, ?, ?, ?, ?)",
            (candidate_id, candidate_task_id, candidate_state, "sha256:" + "9" * 64, "sha256:" + "9" * 64, "work-n4"),
        )
    conn.execute(
        "INSERT INTO n4_publications VALUES (?, ?, ?, ?)",
        ("publication-p3-03", "P3-03", candidate_id, candidate_view_id),
    )
    conn.commit()
    conn.close()


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


@pytest.mark.parametrize(
    ("changed_path", "expected_status"),
    [("src/bar.ts", "FAIL"), ("src/foo.ts", "PASS")],
)
def test_git_delivery_must_match_explicit_deliverable_path(
    tmp_path: Path, changed_path: str, expected_status: str
) -> None:
    task_spec = {
        "id": "P3-03",
        "milestone_id": "m1",
        "title": "Explicit file delivery",
        "description": "change the declared source file",
        "status": "active",
        "dependencies": [],
        "acceptance_criteria": ["test:deterministic"],
        "deliverables": ["src/foo.ts"],
    }
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path, tasks=[task_spec])
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    head_after = _commit_file(repo, changed_path)
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
        "result_summary": "explicit deliverable path verification",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == expected_status
    if expected_status == "FAIL":
        assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_nonexistent_head_before_closes_git_evidence_channel(tmp_path: Path) -> None:
    _, _, _, repo, _ = _fixture(tmp_path)
    head_after = _commit_file(repo, "src/premium/calculator.ts")
    task = ProjectTask(
        task_id="P3-03",
        milestone_id="m1",
        title="Premium Calculation Engine",
        description="implement engine",
        status="active",
        dependencies=(),
        acceptance_criteria=("test:deterministic",),
        deliverables=("src/premium/calculator.ts",),
    )

    verified, reason = verify_authoritative_code_delivery(
        head_before="f" * 40,
        head_after=head_after,
        local_repo_path=repo,
        task=task,
    )
    assert verified is False
    assert reason is not None
    assert "git:head_before_not_found_in_git:" in reason


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


def test_validation_only_for_other_task_candidate_fails(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    _write_candidate_control_db(
        catalog.runtime_root,
        candidates=[("candidate-other", "OTHER-TASK", "SEALED", "sha256:" + "d" * 64, "sha256:" + "d" * 64)],
        validations=[("validation-other", "candidate-other", "PASS")],
    )

    attempt = coordinator.record_result(
        project_id,
        _candidate_result_payload(project_id, binding, head_init, {"validation_id": "validation-other"}),
    )
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_candidate_tree_digest_for_other_task_fails(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    digest = "sha256:" + "e" * 64
    _write_candidate_control_db(
        catalog.runtime_root,
        candidates=[("candidate-other", "OTHER-TASK", "SEALED", digest, digest)],
        validations=[],
    )

    attempt = coordinator.record_result(
        project_id,
        _candidate_result_payload(project_id, binding, head_init, {"candidate_tree_digest": digest}),
    )
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_candidate_with_null_task_id_fails_closed(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    _write_candidate_control_db(
        catalog.runtime_root,
        candidates=[("candidate-unbound", None, "SEALED", "sha256:" + "f" * 64, "sha256:" + "f" * 64)],
        validations=[],
    )

    attempt = coordinator.record_result(
        project_id,
        _candidate_result_payload(project_id, binding, head_init, {"candidate_id": "candidate-unbound"}),
    )
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_candidate_and_validation_must_reference_same_candidate(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    _write_candidate_control_db(
        catalog.runtime_root,
        candidates=[
            ("candidate-p3-03", "P3-03", "SEALED", "sha256:" + "1" * 64, "sha256:" + "1" * 64),
            ("candidate-other", "OTHER-TASK", "SEALED", "sha256:" + "2" * 64, "sha256:" + "2" * 64),
        ],
        validations=[("validation-other", "candidate-other", "PASS")],
    )

    attempt = coordinator.record_result(
        project_id,
        _candidate_result_payload(
            project_id,
            binding,
            head_init,
            {"candidate_id": "candidate-p3-03", "validation_id": "validation-other"},
        ),
    )
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_authoritative_promotion_in_control_db_passes(tmp_path: Path) -> None:
    """Task-bound M7c -> M7a -> candidate promotion evidence succeeds."""
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    _write_task_bound_promotion(catalog.runtime_root, task_id="P3-03")

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


def test_other_task_m7c_promotion_does_not_prove_p3_03(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    _write_task_bound_promotion(catalog.runtime_root, task_id="OTHER-TASK")

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
        "result_summary": "other task promotion must not satisfy P3-03",
        "criteria": [{"criterion": "test:deterministic", "type": "DETERMINISTIC", "status": "PASS"}],
    }

    attempt = coordinator.record_result(project_id, result_payload)
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_n4_publication_without_candidate_is_not_promotion_proof(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    _write_n4_publication_control_db(
        catalog.runtime_root,
        candidate_id=None,
        candidate_view_id=None,
    )

    attempt = coordinator.record_result(
        project_id,
        _promotion_result_payload(project_id, binding, head_init, "publication without a code candidate"),
    )
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_n4_publication_with_other_task_candidate_is_not_promotion_proof(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    _write_n4_publication_control_db(
        catalog.runtime_root,
        candidate_id="candidate-other-task",
        candidate_view_id="view-other-task",
        candidate_task_id="OTHER-TASK",
        candidate_state="SEALED",
    )

    attempt = coordinator.record_result(
        project_id,
        _promotion_result_payload(project_id, binding, head_init, "publication references another task candidate"),
    )
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


def test_unrecognized_m4b_promoted_literal_is_not_promotion_proof(tmp_path: Path) -> None:
    catalog, coordinator, project_id, repo, head_init = _fixture(tmp_path)
    binding = coordinator.start(project_id, expected_repo_head_before=head_init)
    control_dir = catalog.runtime_root / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(control_dir / "control.db")
    conn.execute(
        """CREATE TABLE m4b_candidate_effects (
            candidate_id TEXT PRIMARY KEY,
            task_id TEXT,
            state TEXT,
            observed_tree_digest TEXT,
            planned_tree_digest TEXT,
            work_id TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO m4b_candidate_effects VALUES (?, ?, ?, ?, ?, ?)",
        ("candidate-p3-03", "P3-03", "PROMOTED", "sha256:" + "8" * 64, "sha256:" + "8" * 64, "work-p3-03"),
    )
    conn.commit()
    conn.close()

    attempt = coordinator.record_result(
        project_id,
        _promotion_result_payload(project_id, binding, head_init, "M4b has no canonical PROMOTED state"),
    )
    assert attempt.result_status == "FAIL"
    assert attempt.failure_code == "missing_code_deliverable_evidence"


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
