from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bdb_vnext.project_catalog import (
    ProjectBrief,
    ProjectCatalog,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_execution import ProjectExecutionCoordinator, ProjectExecutionError
from bdb_vnext.project_memory import ProjectMemoryStore
from bdb_vnext.project_workflow import ProjectWorkflow, ProjectWorkflowError


PROJECT_ID = "code-evidence-retry-fixture"
TASK_ID = "P3-03"
OTHER_TASK_ID = "P3-04"
DELIVERABLE = "src/foo.ts"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "BDB Retry Tests")
    _git(repo, "config", "user.email", "bdb-retry-tests@example.invalid")
    (repo / "src").mkdir()
    (repo / DELIVERABLE).write_text("export const amount = 1;\n", encoding="utf-8")
    _git(repo, "add", DELIVERABLE)
    _git(repo, "commit", "--quiet", "-m", "baseline")


def _make_plan(project_id: str):
    document = {
        "schema": "bdb-project-plan-v1",
        "project_id": project_id,
        "project_name": "Code Evidence Retry Fixture",
        "plan_version": 1,
        "milestones": [
            {"id": "P3", "title": "Delivery", "description": "Bounded code delivery", "status": "active"}
        ],
        "tasks": [
            {
                "id": TASK_ID,
                "milestone_id": "P3",
                "title": "Deliver ResultsBreakdown",
                "description": "Verify declared source delivery and acceptance.",
                "status": "active",
                "dependencies": [],
                "acceptance_criteria": ["test:contract"],
                "deliverables": [DELIVERABLE],
            },
            {
                "id": OTHER_TASK_ID,
                "milestone_id": "P3",
                "title": "Unrelated task",
                "description": "A separate task must not be retried through P3-03 evidence.",
                "status": "pending",
                "dependencies": [],
                "acceptance_criteria": ["test:contract"],
                "deliverables": ["src/other.ts"],
            },
        ],
        "current_task_id": TASK_ID,
    }
    return validate_project_plan(document, expected_project_id=project_id)


def _fixture(tmp_path: Path, *, delivery: str = "declared", failed_task: str = TASK_ID):
    runtime = tmp_path / "runtime"
    repo = tmp_path / "premium-calculator"
    _init_repo(repo)
    head_before = _git(repo, "rev-parse", "HEAD")

    delivery_repo = tmp_path / "delivery-source"
    if delivery == "non_ancestor":
        _init_repo(delivery_repo)
        _git(delivery_repo, "checkout", "--quiet", "--orphan", "unrelated-history")
        (delivery_repo / "independent-root.txt").write_text("independent history\n", encoding="utf-8")
        _git(delivery_repo, "add", "--all")
        _git(delivery_repo, "commit", "--quiet", "-m", "independent root")
    else:
        subprocess.run(
            ["git", "clone", "--quiet", str(repo), str(delivery_repo)],
            check=True,
            capture_output=True,
        )
        _git(delivery_repo, "config", "user.name", "BDB Retry Tests")
        _git(delivery_repo, "config", "user.email", "bdb-retry-tests@example.invalid")

    if delivery == "path_mismatch":
        (delivery_repo / "src" / "bar.ts").write_text("export const bar = 2;\n", encoding="utf-8")
        _git(delivery_repo, "add", "src/bar.ts")
    else:
        (delivery_repo / DELIVERABLE).write_text("export const amount = 2;\n", encoding="utf-8")
        _git(delivery_repo, "add", DELIVERABLE)
    _git(delivery_repo, "commit", "--quiet", "-m", "delivery")
    head_after = _git(delivery_repo, "rev-parse", "HEAD")

    # Keep the registered checkout at a different HEAD. The historical retry
    # must retain head_before instead of silently adopting this current HEAD.
    (repo / "README.md").write_text("current checkout advanced independently\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "advance local checkout")
    current_head = _git(repo, "rev-parse", "HEAD")
    assert current_head != head_before

    brief = ProjectBrief(
        "Code Evidence Retry Fixture",
        "Exercise append-only evidence reconciliation.",
        "Temporary Git repository only.",
        "No live Project Memory changes.",
    )
    project = new_project_record(
        project_id=PROJECT_ID,
        display_name="Code Evidence Retry Fixture",
        repo_alias="premium-calculator",
        local_repo_path=repo,
        github_repo="eagleblastmusic-lgtm/premium-calculator",
        brief=brief,
    )
    catalog = ProjectCatalog(runtime)
    catalog.upsert(project)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    plan = memory.ensure_initial_plan(_make_plan(PROJECT_ID))
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
    coordinator = ProjectExecutionCoordinator(str(runtime), catalog=catalog)
    coordinator.begin_milestone_auto(PROJECT_ID, milestone_id="P3", milestone_run_id="retry-run")
    binding = coordinator.start(
        PROJECT_ID,
        task_id=failed_task,
        expected_repo_head_before=head_before,
    )
    result = coordinator.record_result(
        PROJECT_ID,
        {
            "project_id": PROJECT_ID,
            "plan_version": "1",
            "task_id": failed_task,
            "execution_binding_id": binding.execution_binding_id,
            "correlation_id": binding.correlation_id,
            "command_id": binding.command_id,
            "repo_alias": "premium-calculator",
            "head_before": head_before,
            "head_after": head_after,
            "execution_status": "PASS",
            "validation_status": "PASS",
            "promotion_status": "NOT_RUN",
            "criteria": [
                {"criterion": "test:contract", "type": "DETERMINISTIC", "status": "PASS"}
            ],
        },
    )
    workflow = ProjectWorkflow(str(runtime), catalog=catalog)
    return {
        "runtime": runtime,
        "repo": repo,
        "delivery_repo": delivery_repo,
        "head_before": head_before,
        "head_after": head_after,
        "current_head": current_head,
        "catalog": catalog,
        "coordinator": coordinator,
        "workflow": workflow,
        "binding": binding,
        "attempt": result,
    }


def _fetch_delivery(fixture) -> None:
    _git(fixture["repo"], "fetch", str(fixture["delivery_repo"]), "HEAD")


def _retry(fixture, *, task_id: str = TASK_ID, attempt_id: str | None = None, digest: str | None = None):
    return fixture["workflow"].queue_code_evidence_retry(
        PROJECT_ID,
        task_id=task_id,
        attempt_id=attempt_id or fixture["attempt"].attempt_id,
        expected_result_digest=digest or fixture["attempt"].result_digest,
    )


def test_auto_retries_failed_code_evidence_with_inherited_window_and_new_generation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original_binding = fixture["coordinator"].binding(PROJECT_ID, fixture["binding"].execution_binding_id)
    original_attempt = fixture["coordinator"].snapshot(PROJECT_ID)["attempts"][-1]
    _fetch_delivery(fixture)
    launch, status = fixture["workflow"].ensure_auto_current_launch(PROJECT_ID)

    assert status == "ready"
    assert launch is not None and launch.auto_send is True
    assert launch.plan_version == "1"
    assert launch.task_id == TASK_ID
    assert launch.expected_repo_head_before == fixture["head_before"]
    assert launch.execution_binding_id != fixture["binding"].execution_binding_id

    snapshot = fixture["coordinator"].snapshot(PROJECT_ID)
    retried = fixture["coordinator"].binding(PROJECT_ID, launch.execution_binding_id)
    assert retried.generation == original_binding.generation + 1
    assert retried.expected_repo_head_before == fixture["head_before"]
    assert snapshot["attempts"][0] == original_attempt
    assert snapshot["bindings"][0] == original_binding.to_dict()
    assert len(snapshot["code_evidence_retries"]) == 1
    assert snapshot["code_evidence_retries"][0]["prior_result_digest"] == fixture["attempt"].result_digest
    assert snapshot["launch_outbox"][launch.launch_id]["auto_send"] is True


def test_explicit_continue_uses_evidence_retry_instead_of_current_head(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _fetch_delivery(fixture)

    launch = fixture["workflow"].queue_continue_prompt(PROJECT_ID)

    assert launch.task_id == TASK_ID
    assert launch.expected_repo_head_before == fixture["head_before"]
    assert launch.expected_repo_head_before != fixture["current_head"]
    assert launch.execution_binding_id != fixture["binding"].execution_binding_id
    assert launch.auto_send is True


def test_generic_start_cannot_override_failed_evidence_baseline(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(ProjectExecutionError) as exc_info:
        fixture["coordinator"].start(
            PROJECT_ID,
            task_id=TASK_ID,
            expected_repo_head_before=fixture["current_head"],
        )

    assert exc_info.value.code == "evidence_retry_required"
    snapshot = fixture["coordinator"].snapshot(PROJECT_ID)
    assert len(snapshot["bindings"]) == 1
    assert len(snapshot["code_evidence_retries"]) == 0


@pytest.mark.parametrize(
    ("kwargs", "error_code"),
    [
        ({"attempt_id": "attempt-does-not-exist"}, "evidence_retry_attempt_not_found"),
        ({"digest": "sha256:" + "0" * 64}, "evidence_retry_digest_mismatch"),
    ],
)
def test_retry_requires_exact_attempt_and_result_digest(tmp_path: Path, kwargs, error_code: str) -> None:
    fixture = _fixture(tmp_path)
    _fetch_delivery(fixture)
    with pytest.raises(ProjectWorkflowError) as exc_info:
        _retry(fixture, **kwargs)
    assert exc_info.value.code == error_code
    snapshot = fixture["coordinator"].snapshot(PROJECT_ID)
    assert len(snapshot["code_evidence_retries"]) == 0
    assert len(snapshot["bindings"]) == 1


def test_unrelated_failed_task_cannot_be_retried_as_p3_03(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, failed_task=OTHER_TASK_ID)
    _fetch_delivery(fixture)
    with pytest.raises(ProjectWorkflowError) as exc_info:
        _retry(fixture, task_id=TASK_ID)
    assert exc_info.value.code == "evidence_retry_attempt_task_mismatch"
    snapshot = fixture["coordinator"].snapshot(PROJECT_ID)
    assert len(snapshot["code_evidence_retries"]) == 0


@pytest.mark.parametrize(
    ("delivery", "diagnostic"),
    [
        ("missing", "head_after_not_found_in_git"),
        ("non_ancestor", "head_after_not_ancestor"),
        ("path_mismatch", "git_diff_lacks_declared_code_path"),
    ],
)
def test_retry_fails_closed_until_exact_git_delivery_window_is_verifiable(
    tmp_path: Path,
    delivery: str,
    diagnostic: str,
) -> None:
    fixture = _fixture(tmp_path, delivery="declared" if delivery == "missing" else delivery)
    if delivery != "missing":
        _fetch_delivery(fixture)

    with pytest.raises(ProjectWorkflowError) as exc_info:
        _retry(fixture)
    assert exc_info.value.code == "evidence_retry_git_delivery_unverified"
    assert diagnostic in str(exc_info.value)
    snapshot = fixture["coordinator"].snapshot(PROJECT_ID)
    assert snapshot["task_statuses"][TASK_ID] == "active"
    assert len(snapshot["code_evidence_retries"]) == 0
    assert len(snapshot["bindings"]) == 1


def test_retry_becomes_available_when_delivery_object_is_locally_observable(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _fetch_delivery(fixture)

    launch, status = _retry(fixture)

    assert status == "ready"
    assert launch is not None
    assert launch.expected_repo_head_before == fixture["head_before"]
    assert fixture["coordinator"].snapshot(PROJECT_ID)["current_binding_id"] == launch.execution_binding_id


def test_duplicate_retry_is_idempotent_and_auto_reuses_same_launch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _fetch_delivery(fixture)

    first, first_status = _retry(fixture)
    second, second_status = _retry(fixture)

    assert first_status == "ready"
    assert second_status in {"ready", "reused"}
    assert first is not None and second is not None
    assert second.execution_binding_id == first.execution_binding_id
    assert second.launch_id == first.launch_id
    snapshot = fixture["coordinator"].snapshot(PROJECT_ID)
    assert len(snapshot["code_evidence_retries"]) == 1
    assert len([b for b in snapshot["bindings"] if b.get("task_id") == TASK_ID]) == 2


def test_failure_diagnostic_preserves_git_verification_reason(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    snapshot = fixture["coordinator"].snapshot(PROJECT_ID)
    acceptance = snapshot["acceptance_results"][-1]
    criterion = next(item for item in acceptance["criteria"] if item["criterion"] == "canonical:code_delivery_evidence")

    assert fixture["attempt"].failure_code == "missing_code_deliverable_evidence"
    assert "head_after_not_found_in_git" in criterion["evidence_ref"]
