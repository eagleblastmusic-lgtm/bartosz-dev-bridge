"""Regression fixtures for canonical Project Center AUTO prerequisites."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from bdb_vnext.auto_scope_contract import AutoScope
from bdb_vnext.project_catalog import (
    ProjectBrief,
    ProjectCatalogError,
    ProjectMilestone,
    ProjectPlan,
    new_project_record,
    validate_project_plan,
)
from bdb_vnext.project_center_auto import (
    CanonicalProjectCenterAutoCommands,
    ProjectCenterAutoCommandError,
    ProjectCenterAutoViewModel,
)
from bdb_vnext.project_memory import ProjectMemoryStore, available_project_tasks, resolve_next_action
from bdb_vnext.scope_orchestrator import (
    CanonicalPlanGraph,
    PlanMilestoneNode,
    PlanPrerequisiteNode,
    PlanTaskNode,
    ScopeAction,
    ScopeOrchestrator,
)
from bdb_vnext.until_stopped import UntilStoppedController


PROJECT_ID = "premium-calculator-fixture"


def _premium_prerequisite_plan() -> ProjectPlan:
    document = {
        "schema": "bdb-project-plan-v1",
        "project_id": PROJECT_ID,
        "project_name": "Premium Calculator",
        "plan_version": "1",
        "milestones": [
            {"id": "P1", "title": "P1", "description": "P1 milestone", "status": "active"},
        ],
        "tasks": [
            {"id": "P0-01", "milestone_id": "P1", "title": "P0-01", "description": "Completed P0 task", "status": "completed", "dependencies": [], "acceptance_criteria": []},
            {"id": "P1-01", "milestone_id": "P1", "title": "P1-01", "description": "First P1 task", "status": "pending", "dependencies": ["G0"], "acceptance_criteria": []},
        ],
        "current_task_id": "P1-01",
        "planning_context": {
            "gates": [{"id": "G0", "title": "Foundation gate", "criteria": "Foundation is accepted."}],
        },
    }
    return validate_project_plan(document, expected_project_id=PROJECT_ID)


def _premium_two_milestone_plan() -> ProjectPlan:
    document = {
        "schema": "bdb-project-plan-v1",
        "project_id": PROJECT_ID,
        "project_name": "Premium Calculator",
        "plan_version": "1",
        "milestones": [
            {"id": "P0", "title": "P0", "description": "P0 milestone", "status": "completed"},
            {"id": "P1", "title": "P1", "description": "P1 milestone", "status": "active"},
        ],
        "tasks": [
            {"id": "P0-01", "milestone_id": "P0", "title": "P0-01", "description": "Completed P0 task", "status": "completed", "dependencies": [], "acceptance_criteria": []},
            {"id": "P1-01", "milestone_id": "P1", "title": "P1-01", "description": "First P1 task", "status": "pending", "dependencies": ["G0"], "acceptance_criteria": []},
        ],
        "current_task_id": "P1-01",
        "planning_context": {
            "gates": [{"id": "G0", "title": "Foundation gate", "criteria": "Foundation is accepted."}],
        },
    }
    return validate_project_plan(document, expected_project_id=PROJECT_ID)


def _adapter(runtime_root: Path, plan: ProjectPlan) -> CanonicalProjectCenterAutoCommands:
    record = new_project_record(
        project_id=PROJECT_ID,
        display_name=plan.project_name,
        repo_alias="premium-calculator",
        local_repo_path=runtime_root / "repo",
        github_repo=None,
        brief=ProjectBrief("Premium Calculator", "Calculate premiums", "Fixture", "tool"),
    )
    record = replace(
        record,
        plan_imported=True,
        plan_version=plan.plan_version,
        total_tasks=len(plan.tasks),
        completed_tasks=1,
        current_milestone="P1",
        current_task="P1-01",
    )
    ProjectMemoryStore(runtime_root, PROJECT_ID).ensure_initial_plan(plan)
    return CanonicalProjectCenterAutoCommands(
        runtime_root,
        PROJECT_ID,
        project_provider=lambda: record,
        plan_provider=lambda: plan,
    )


def _dependency_plan(dependency: str, *, planning_context: dict[str, object] | None = None) -> ProjectPlan:
    document: dict[str, object] = {
        "schema": "bdb-project-plan-v1",
        "project_id": PROJECT_ID,
        "project_name": "Prerequisite fixture",
        "plan_version": "1",
        "milestones": [
            {"id": "P1", "title": "P1", "description": "P1 milestone", "status": "active"},
        ],
        "tasks": [
            {"id": "P0-01", "milestone_id": "P1", "title": "P0-01", "description": "Completed task", "status": "completed", "dependencies": [], "acceptance_criteria": []},
            {"id": "P1-01", "milestone_id": "P1", "title": "P1-01", "description": "Dependent task", "status": "pending", "dependencies": [dependency], "acceptance_criteria": []},
        ],
        "current_task_id": "P1-01",
    }
    if planning_context is not None:
        document["planning_context"] = planning_context
    return validate_project_plan(document, expected_project_id=PROJECT_ID)


def _persisted_cursor_explanation(adapter: CanonicalProjectCenterAutoCommands) -> dict[str, object]:
    connection = sqlite3.connect(str(adapter.db_path))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT explanation_json FROM scope_cursors WHERE project_id = ?",
            (PROJECT_ID,),
        ).fetchone()
        assert row is not None
        return json.loads(row["explanation_json"])
    finally:
        connection.close()



def test_snapshot_does_not_project_future_task_prerequisites_onto_current_task(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    document = {
        "schema": "bdb-project-plan-v1",
        "project_id": PROJECT_ID,
        "project_name": "Premium Calculator",
        "plan_version": "1",
        "milestones": [
            {"id": "P1", "title": "P1", "description": "P1 milestone", "status": "active"},
        ],
        "tasks": [
            {
                "id": "P0-01",
                "milestone_id": "P1",
                "title": "Completed dependency",
                "description": "Completed dependency",
                "status": "completed",
                "dependencies": [],
                "acceptance_criteria": [],
            },
            {
                "id": "P1-01",
                "milestone_id": "P1",
                "title": "Current task",
                "description": "Current runnable task",
                "status": "pending",
                "dependencies": ["P0-01"],
                "acceptance_criteria": [],
            },
            {
                "id": "P1-02",
                "milestone_id": "P1",
                "title": "Future task",
                "description": "Future task with future prerequisites",
                "status": "pending",
                "dependencies": ["G5", "OQ-003"],
                "acceptance_criteria": [],
            },
        ],
        "current_task_id": "P1-01",
        "planning_context": {
            "gates": [
                {
                    "id": "G5",
                    "title": "Future product-quality gate",
                    "criteria": "Future task gate only.",
                }
            ],
            "open_questions": [
                {
                    "id": "OQ-003",
                    "question": "Future release question?",
                }
            ],
        },
    }
    plan = validate_project_plan(document, expected_project_id=PROJECT_ID)
    adapter = _adapter(runtime, plan)

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    runnable = adapter.continue_auto()
    state = adapter.snapshot(plan_available=True)

    assert runnable.reason_code == "MILESTONE_NEXT_TASK"
    assert state.current_task_id == "P1-01"
    assert state.planning_gate_id is None
    assert state.planning_gate_status is None
    assert state.open_question_id is None
    assert state.open_question_status is None


def test_isolated_two_milestone_case_reproduces_independent_pending_boundaries(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    plan = _premium_two_milestone_plan()
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    record = new_project_record(
        project_id=PROJECT_ID,
        display_name=plan.project_name,
        repo_alias="premium-calculator",
        local_repo_path=runtime / "repo",
        github_repo=None,
        brief=ProjectBrief("Premium Calculator", "Calculate premiums", "Fixture", "tool"),
    )
    record = replace(
        record,
        plan_imported=True,
        plan_version=plan.plan_version,
        total_tasks=len(plan.tasks),
        completed_tasks=1,
        current_milestone="P1",
        current_task="P1-01",
    )

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    receipt = adapter.continue_auto()
    state = memory.read_state()

    assert receipt.reason_code == "MILESTONE_GATE_PENDING"
    assert receipt.current_milestone_id == "P0"
    assert resolve_next_action(record, plan, state).code == "GATE_REQUIRED"
    assert state.execution["gate_statuses"] == {"G0": "pending"}
    assert available_project_tasks(plan, state) == ()


def test_milestone_gate_lifecycle_is_durable_and_next_scope_is_explicit(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    plan = _premium_two_milestone_plan()
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    pending = adapter.continue_auto()
    before_approval = memory.read_state()
    assert pending.reason_code == "MILESTONE_GATE_PENDING"
    assert pending.current_task_id is None
    assert before_approval.execution["milestone_gate_statuses"] == {"GATE:P0": "pending", "GATE:P1": "pending"}

    approved = adapter.pass_milestone_gate("GATE:P0", expected_revision=before_approval.revision)
    after_approval = memory.read_state()
    assert approved.reason_code == "MILESTONE_GATE_PASSED"
    assert after_approval.execution["milestone_gate_statuses"]["GATE:P0"] == "passed"
    assert after_approval.revision == before_approval.revision + 1

    restarted = CanonicalProjectCenterAutoCommands(
        runtime,
        PROJECT_ID,
        plan_provider=lambda: plan,
    )
    restarted_state = restarted.snapshot(plan_available=True)
    assert restarted_state.milestone_gate_status == "passed"
    assert restarted_state.prerequisite_revision == after_approval.revision

    completed = restarted.continue_auto()
    completed_state = restarted.snapshot(plan_available=True)
    assert completed.reason_code == "MILESTONE_SCOPE_COMPLETED"
    assert completed_state.scope_status == "COMPLETED"
    assert completed_state.current_milestone_id == "P0"
    assert completed_state.next_milestone_id == "P1"
    old_run_id = completed_state.run_id
    old_epoch = completed_state.scope_epoch

    next_started = adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    next_state = adapter.snapshot(plan_available=True)
    assert next_started.reason_code == "AUTO_STARTED_NEXT_MILESTONE"
    assert next_state.scope_status == "ACTIVE"
    assert next_state.current_milestone_id == "P1"
    assert next_state.current_task_id is None
    assert next_state.run_id != old_run_id
    assert next_state.scope_epoch == old_epoch + 1

    waiting = adapter.continue_auto()
    assert waiting.reason_code == "MILESTONE_TASK_DEPENDENCY_PENDING"
    assert waiting.current_milestone_id == "P1"
    assert waiting.current_task_id is None
    memory.pass_gate("G0")
    runnable = adapter.continue_auto()
    assert runnable.reason_code == "MILESTONE_NEXT_TASK"
    assert runnable.current_milestone_id == "P1"
    assert runnable.current_task_id == "P1-01"

    connection = sqlite3.connect(str(adapter.db_path))
    try:
        histories = connection.execute(
            "SELECT run_id, status FROM runs WHERE project_id = ? ORDER BY started_at",
            (PROJECT_ID,),
        ).fetchall()
    finally:
        connection.close()
    assert len(histories) == 2
    assert histories[0][0] == old_run_id and histories[0][1] == "completed"
    assert histories[1][0] == next_state.run_id and histories[1][1] == "running"

    duplicate_before = memory.read_state()
    duplicate = adapter.pass_milestone_gate("GATE:P0")
    duplicate_after = memory.read_state()
    assert duplicate.idempotent is True
    assert duplicate_before.to_dict() == duplicate_after.to_dict()


@pytest.mark.parametrize("scope", (AutoScope.PROJECT, AutoScope.UNTIL_STOPPED))
def test_long_lived_scope_crosses_milestone_without_history_identity_conflict(
    tmp_path: Path,
    scope: AutoScope,
) -> None:
    runtime = tmp_path / "runtime"
    document = _premium_two_milestone_plan().to_dict()
    document["tasks"][1]["dependencies"] = []
    document.pop("planning_context", None)
    plan = validate_project_plan(document, expected_project_id=PROJECT_ID)
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)

    adapter.start_auto(scope, confirmed=True)
    pending = adapter.continue_auto()
    assert pending.reason_code in {"MILESTONE_GATE_PENDING", "MILESTONE_GATE_REQUIRED_BEFORE_ADVANCING"}

    memory.pass_milestone_gate("GATE:P0")
    crossed = adapter.continue_auto()
    assert crossed.current_milestone_id == "P1"
    assert crossed.current_task_id == "P1-01"

    connection = sqlite3.connect(str(adapter.db_path))
    try:
        run_history = connection.execute(
            "SELECT run_id, project_id, milestone_id FROM runs WHERE project_id = ?",
            (PROJECT_ID,),
        ).fetchall()
        scope_history = connection.execute(
            "SELECT project_id, mode, milestone_id, status FROM scopes WHERE project_id = ?",
            (PROJECT_ID,),
        ).fetchall()
    finally:
        connection.close()
    assert len(run_history) == 1
    assert run_history[0][1:] == (PROJECT_ID, "P0")
    assert scope_history == [(PROJECT_ID, scope.value, "P1", "RUNNING")]


@pytest.mark.parametrize("scope", (AutoScope.PROJECT, AutoScope.UNTIL_STOPPED))
def test_long_lived_scope_waits_for_next_task_prerequisite_after_gate(
    tmp_path: Path,
    scope: AutoScope,
) -> None:
    runtime = tmp_path / "runtime"
    adapter = _adapter(runtime, _premium_two_milestone_plan())
    memory = ProjectMemoryStore(runtime, PROJECT_ID)

    adapter.start_auto(scope, confirmed=True)
    adapter.continue_auto()
    memory.pass_milestone_gate("GATE:P0")

    waiting = adapter.continue_auto()
    assert waiting.reason_code == "NEXT_MILESTONE_TASK_DEPENDENCY_PENDING"
    assert waiting.current_milestone_id == "P1"
    assert waiting.current_task_id is None
    assert _persisted_cursor_explanation(adapter)["dependency_evidence"] == {"G0": "pending"}

    memory.pass_gate("G0")
    runnable = adapter.continue_auto()
    assert runnable.reason_code == "PROJECT_NEXT_TASK"
    assert runnable.current_milestone_id == "P1"
    assert runnable.current_task_id == "P1-01"


def test_completed_last_milestone_does_not_create_phantom_next_run(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    document = _premium_two_milestone_plan().to_dict()
    document["milestones"] = [document["milestones"][0]]
    document["tasks"] = [document["tasks"][0]]
    document["current_task_id"] = "P0-01"
    document.pop("planning_context", None)
    plan = validate_project_plan(document, expected_project_id=PROJECT_ID)
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    pending = adapter.continue_auto()
    assert pending.reason_code == "MILESTONE_GATE_PENDING"
    pending_state = memory.read_state()
    memory.pass_milestone_gate("GATE:P0", expected_revision=pending_state.revision)
    completed = adapter.continue_auto()
    before = adapter.snapshot(plan_available=True)
    assert completed.reason_code == "MILESTONE_SCOPE_COMPLETED"
    assert before.scope_status == "COMPLETED"

    with pytest.raises(ProjectCenterAutoCommandError) as error:
        adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    assert error.value.code == "no_next_milestone"
    after = adapter.snapshot(plan_available=True)
    assert after.run_id == before.run_id
    assert after.scope_epoch == before.scope_epoch
    assert after.current_milestone_id == "P0"

    connection = sqlite3.connect(str(adapter.db_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs WHERE project_id = ?", (PROJECT_ID,)).fetchone()[0] == 1
    finally:
        connection.close()


def test_milestone_gate_commands_fail_closed_for_unknown_and_stale_state(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    adapter = _adapter(runtime, _premium_two_milestone_plan())
    memory = ProjectMemoryStore(runtime, PROJECT_ID)

    with pytest.raises(ProjectCenterAutoCommandError) as unknown:
        adapter.pass_milestone_gate("P0")
    assert unknown.value.code == "milestone_gate_not_found"

    state = memory.read_state()
    memory.pass_gate("G0")
    with pytest.raises(ProjectCenterAutoCommandError) as stale:
        adapter.pass_milestone_gate("GATE:P0", expected_revision=state.revision)
    assert stale.value.code == "stale_prerequisite"
    with pytest.raises(ProjectCenterAutoCommandError) as unknown_planning:
        adapter.pass_gate("missing-gate")
    assert unknown_planning.value.code == "prerequisite_not_found"


def test_auto_rejects_ambiguous_milestone_and_planning_gate_mapping(tmp_path: Path) -> None:
    document = _premium_two_milestone_plan().to_dict()
    document["tasks"][1]["dependencies"] = ["GATE:P0"]
    document["planning_context"]["gates"] = [
        {"id": "GATE:P0", "title": "Collision", "criteria": "No implicit mapping"},
    ]
    plan = validate_project_plan(document, expected_project_id=PROJECT_ID)
    adapter = _adapter(tmp_path / "runtime", plan)

    with pytest.raises(ProjectCenterAutoCommandError) as error:
        adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    assert error.value.code == "ambiguous_gate_mapping"
    assert not adapter.db_path.exists()


def test_auto_rejects_malformed_durable_milestone_gate_state(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    plan = _premium_two_milestone_plan()
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    state = memory.read_state()
    malformed = replace(
        state,
        execution={
            **state.execution,
            "milestone_gate_statuses": {"GATE:P0": "passed", "UNKNOWN": "passed"},
        },
    )
    memory.execution_transaction(lambda _state: (malformed, None))

    with pytest.raises(ProjectCenterAutoCommandError) as error:
        adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    assert error.value.code == "milestone_gate_state_invalid"
    assert not adapter.db_path.exists()


@pytest.mark.parametrize(
    ("action", "map_key", "malformed", "error_code", "identifier"),
    (
        ("gate", "gate_statuses", {"G0": "passed", "UNKNOWN": "pending"}, "planning_gate_state_invalid", "G0"),
        ("open_question", "open_question_statuses", {"OQ-001": "resolved", "UNKNOWN": "open"}, "open_question_state_invalid", "OQ-001"),
    ),
)
def test_auto_rejects_malformed_planning_prerequisite_state_without_mutation(
    tmp_path: Path,
    action: str,
    map_key: str,
    malformed: dict[str, str],
    error_code: str,
    identifier: str,
) -> None:
    runtime = tmp_path / "runtime"
    plan = _dependency_plan(
        "G0",
        planning_context={
            "gates": [{"id": "G0", "title": "Foundation gate", "criteria": "Foundation is accepted."}],
            "open_questions": [{"id": "OQ-001", "question": "Which release cadence?"}],
        },
    )
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    state = memory.read_state()
    malformed_state = replace(state, execution={**state.execution, map_key: malformed})
    memory.execution_transaction(lambda _state: (malformed_state, None))
    before = memory.read_state().to_dict()

    with pytest.raises(ProjectCenterAutoCommandError) as error:
        if action == "gate":
            adapter.pass_gate(identifier)
        else:
            adapter.resolve_open_question(identifier)

    assert error.value.code == error_code
    assert memory.read_state().to_dict() == before


def test_legacy_state_without_milestone_gate_entry_is_pending_and_upgradable(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    plan = _premium_two_milestone_plan()
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    adapter.continue_auto()

    state = memory.read_state()
    legacy_state = replace(
        state,
        execution={key: value for key, value in state.execution.items() if key != "milestone_gate_statuses"},
    )
    memory.execution_transaction(lambda _state: (legacy_state, None))
    record = new_project_record(
        project_id=PROJECT_ID,
        display_name=plan.project_name,
        repo_alias="premium-calculator",
        local_repo_path=runtime / "repo",
        github_repo=None,
        brief=ProjectBrief("Premium Calculator", "Calculate premiums", "Fixture", "tool"),
    )
    restarted = CanonicalProjectCenterAutoCommands(
        runtime,
        PROJECT_ID,
        project_provider=lambda: record,
        plan_provider=lambda: plan,
    )

    reloaded = restarted.snapshot(plan_available=True)
    assert reloaded.prerequisite_error is None
    assert reloaded.milestone_gate_id == "GATE:P0"
    assert reloaded.milestone_gate_status == "pending"
    assert reloaded.current_milestone_id == "P0"
    approved = restarted.pass_milestone_gate("GATE:P0", expected_revision=reloaded.prerequisite_revision)
    assert approved.reason_code == "MILESTONE_GATE_PASSED"
    assert ProjectMemoryStore(runtime, PROJECT_ID).read_state().execution["milestone_gate_statuses"]["GATE:P0"] == "passed"
    recovered = CanonicalProjectCenterAutoCommands(
        runtime,
        PROJECT_ID,
        project_provider=lambda: record,
        plan_provider=lambda: plan,
    )
    assert recovered.continue_auto().reason_code == "MILESTONE_SCOPE_COMPLETED"


def test_project_center_gate_actions_confirm_recheck_and_use_workflow_boundary(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from bdb_vnext.control_center_query import ControlCenterSnapshot
    from bdb_vnext.project_catalog import ProjectCatalog
    from bdb_vnext.project_workflow import ProjectWorkflow
    from bdb_gui.project_center import ProjectCenterWindow

    runtime = tmp_path / "runtime"
    plan = _premium_two_milestone_plan()
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)
    record = new_project_record(
        project_id=PROJECT_ID,
        display_name=plan.project_name,
        repo_alias="premium-calculator",
        local_repo_path=runtime / "repo",
        github_repo=None,
        brief=ProjectBrief("Premium Calculator", "Calculate premiums", "Fixture", "tool"),
    )
    record = replace(record, plan_imported=True, plan_version=plan.plan_version, total_tasks=2, current_milestone="P1", current_task="P1-01")
    catalog = ProjectCatalog(runtime)
    catalog.upsert(record)
    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    adapter.continue_auto()

    confirmations: list[bool] = [False]
    app = QApplication.instance() or QApplication(["gate-action-test"])
    window = ProjectCenterWindow(
        runtime_root=runtime,
        catalog=catalog,
        workflow=ProjectWorkflow(runtime, catalog=catalog),
        auto_commands_factory=lambda _project: adapter,
        auto_gate_confirmation=lambda _kind, _identifier, _description: confirmations[0],
        snapshot_loader=lambda root: ControlCenterSnapshot(str(root), "OFF", "OFF", "OFF", "OFF", None, (), ()),
    )
    window._projects = (record,)
    window._select_project(PROJECT_ID)
    assert "GATE:P0" in window._auto_milestone_gate_label.text()
    assert "G0" in window._auto_planning_gate_label.text()
    assert window._auto_milestone_gate_button.isEnabled()
    assert window._auto_planning_gate_button.isEnabled()

    before_cancel = memory.read_state().to_dict()
    window._auto_milestone_gate_button.click()
    assert memory.read_state().to_dict() == before_cancel

    memory.pass_gate("G0")
    confirmations[0] = True
    window._auto_milestone_gate_button.click()
    assert memory.read_state().execution["milestone_gate_statuses"]["GATE:P0"] == "pending"

    window._auto_milestone_gate_button.click()
    assert memory.read_state().execution["milestone_gate_statuses"]["GATE:P0"] == "passed"
    assert window._mutation_operations_invoked == 1
    window.close()
    app.processEvents()


def test_context_gate_dependency_is_not_reported_as_missing_by_auto(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "runtime", _premium_prerequisite_plan())

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    receipt = adapter.continue_auto()

    assert receipt.reason_code == "MILESTONE_TASK_DEPENDENCY_PENDING"
    assert receipt.reason_code != "AMBIGUOUS_PLAN_GRAPH"


def test_auto_task_dependency_uses_canonical_task_status(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "runtime", _dependency_plan("P0-01"))

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    receipt = adapter.continue_auto()

    assert receipt.reason_code == "MILESTONE_NEXT_TASK"
    assert receipt.current_task_id == "P1-01"


def test_auto_gate_dependency_reads_project_memory_pending_then_passed(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    plan = _dependency_plan(
        "G0",
        planning_context={
            "gates": [{"id": "G0", "title": "Foundation gate", "criteria": "Foundation is accepted."}],
        },
    )
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    pending = adapter.continue_auto()

    assert pending.reason_code == "MILESTONE_TASK_DEPENDENCY_PENDING"
    assert adapter.snapshot(plan_available=True).scope_status == "WAITING"
    explanation = _persisted_cursor_explanation(adapter)
    assert explanation["dependency_evidence"] == {"G0": "pending"}
    assert memory.read_state().execution["gate_statuses"] == {"G0": "pending"}

    memory.pass_gate("G0")
    runnable = adapter.continue_auto()

    assert runnable.reason_code == "MILESTONE_NEXT_TASK"
    assert runnable.current_task_id == "P1-01"
    assert _persisted_cursor_explanation(adapter)["dependency_evidence"] == {}


def test_auto_open_question_dependency_reads_project_memory_open_then_resolved(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    plan = _dependency_plan(
        "OQ-001",
        planning_context={
            "open_questions": [{"id": "OQ-001", "question": "Which release cadence?"}],
        },
    )
    adapter = _adapter(runtime, plan)
    memory = ProjectMemoryStore(runtime, PROJECT_ID)

    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)
    pending = adapter.continue_auto()

    assert pending.reason_code == "MILESTONE_TASK_DEPENDENCY_PENDING"
    assert _persisted_cursor_explanation(adapter)["dependency_evidence"] == {"OQ-001": "open"}
    assert memory.read_state().execution["open_question_statuses"] == {"OQ-001": "open"}

    memory.resolve_open_question("OQ-001")
    runnable = adapter.continue_auto()

    assert runnable.reason_code == "MILESTONE_NEXT_TASK"
    assert runnable.current_task_id == "P1-01"


def test_auto_missing_dependency_fails_closed_and_persists_blocked_projection(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    adapter = _adapter(runtime, _dependency_plan("P0-01"))
    adapter.start_auto(AutoScope.MILESTONE, confirmed=True)

    invalid_graph = CanonicalPlanGraph(
        plan_identity=f"{PROJECT_ID}:plan:v1",
        plan_version=1,
        milestones=(PlanMilestoneNode("P1", "GATE:P1", task_ids=("P1-01",)),),
        tasks=(PlanTaskNode("P1-01", "P1", dependencies=("MISSING",)),),
    )
    connection = sqlite3.connect(str(adapter.db_path))
    connection.row_factory = sqlite3.Row
    try:
        orchestrator = ScopeOrchestrator(connection, PROJECT_ID)
        cursor = orchestrator.get_or_create_cursor(
            "run:missing",
            scope=AutoScope.MILESTONE,
            plan_identity=invalid_graph.plan_identity,
            plan_version=1,
            scope_selection_explicit=True,
        )
        decision, explanation, updated = orchestrator.tick(
            invalid_graph,
            cursor,
            {"P1-01": "NOT_STARTED"},
            {"GATE:P1": "NOT_REACHED"},
        )
        assert decision.action == ScopeAction.HALT_BLOCKED
        assert decision.reason_code == "AMBIGUOUS_PLAN_GRAPH"
        assert explanation.reason_code == "AMBIGUOUS_PLAN_GRAPH"
        assert updated.status == "BLOCKED"
        assert updated.disposition == "HALT_BLOCKED"
        assert orchestrator.update_cursor_cas(updated, cursor.state_revision)
    finally:
        connection.close()

    state = adapter.snapshot(plan_available=True)
    view = ProjectCenterAutoViewModel.from_canonical(state)
    assert state.scope_status == "BLOCKED"
    assert state.reason_code == "AMBIGUOUS_PLAN_GRAPH"
    assert view.can_continue is False
    with pytest.raises(ProjectCenterAutoCommandError) as error:
        adapter.continue_auto()
    assert error.value.code == "auto_blocked"


def test_auto_ambiguous_dependency_namespace_fails_closed() -> None:
    document = _premium_prerequisite_plan().to_dict()
    document["tasks"][1]["dependencies"] = ["P0-01"]
    document["planning_context"]["gates"].append(
        {"id": "P0-01", "title": "Collision", "criteria": "ambiguous namespace"}
    )

    with pytest.raises(ProjectCatalogError) as error:
        validate_project_plan(document, expected_project_id=PROJECT_ID)
    assert error.value.code == "plan_dependency_ambiguous"

    graph = CanonicalPlanGraph(
        plan_identity="plan:ambiguous",
        plan_version=1,
        milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T0", "T1")),),
        tasks=(
            PlanTaskNode("T0", "M1"),
            PlanTaskNode("T1", "M1", dependencies=("T0",)),
        ),
        prerequisites=(PlanPrerequisiteNode("T0", "gate"),),
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        orchestrator = ScopeOrchestrator(connection, "ambiguous")
        cursor = orchestrator.get_or_create_cursor("run:ambiguous", scope=AutoScope.MILESTONE)
        decision, _, updated = orchestrator.tick(graph, cursor, {"T0": "NOT_STARTED", "T1": "NOT_STARTED"}, {"G1": "NOT_REACHED"})
        assert decision.action == ScopeAction.HALT_BLOCKED
        assert decision.reason_code == "AMBIGUOUS_PLAN_GRAPH"
        assert updated.status == "BLOCKED"
    finally:
        connection.close()


@pytest.mark.parametrize(
    "milestone",
    [
        PlanMilestoneNode("M1", "G1", task_ids=("MISSING",)),
        PlanMilestoneNode("M1", "G1", dependencies=("MISSING",), task_ids=("T1",)),
    ],
)
def test_auto_missing_milestone_reference_fails_closed(milestone: PlanMilestoneNode) -> None:
    graph = CanonicalPlanGraph(
        plan_identity="plan:missing-milestone-reference",
        plan_version=1,
        milestones=(milestone,),
        tasks=(PlanTaskNode("T1", "M1"),),
    )
    valid, _ = graph.validate_graph()
    assert valid is False

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        orchestrator = ScopeOrchestrator(connection, "missing-milestone-reference")
        cursor = orchestrator.get_or_create_cursor("run:missing-milestone-reference", scope=AutoScope.MILESTONE)
        decision, _, updated = orchestrator.tick(
            graph,
            cursor,
            {"T1": "NOT_STARTED"},
            {"G1": "NOT_REACHED"},
        )
        assert decision.action == ScopeAction.HALT_BLOCKED
        assert decision.reason_code == "AMBIGUOUS_PLAN_GRAPH"
        assert updated.status == "BLOCKED"
    finally:
        connection.close()


def test_auto_milestone_gate_namespace_collision_fails_closed() -> None:
    graph = CanonicalPlanGraph(
        plan_identity="plan:gate-collision",
        plan_version=1,
        milestones=(PlanMilestoneNode("M1", "T0", task_ids=("T0",)),),
        tasks=(PlanTaskNode("T0", "M1"),),
    )

    valid, _ = graph.validate_graph()
    assert valid is False

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        orchestrator = ScopeOrchestrator(connection, "gate-collision")
        cursor = orchestrator.get_or_create_cursor("run:gate-collision", scope=AutoScope.MILESTONE)
        decision, _, updated = orchestrator.tick(
            graph,
            cursor,
            {"T0": "NOT_STARTED"},
            {"T0": "NOT_REACHED"},
        )
        assert decision.action == ScopeAction.HALT_BLOCKED
        assert decision.reason_code == "AMBIGUOUS_PLAN_GRAPH"
        assert updated.status == "BLOCKED"
    finally:
        connection.close()


def test_auto_task_cycle_fails_closed() -> None:
    graph = CanonicalPlanGraph(
        plan_identity="plan:cycle",
        plan_version=1,
        milestones=(PlanMilestoneNode("M1", "G1", task_ids=("T1", "T2")),),
        tasks=(
            PlanTaskNode("T1", "M1", dependencies=("T2",)),
            PlanTaskNode("T2", "M1", dependencies=("T1",)),
        ),
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        orchestrator = ScopeOrchestrator(connection, "cycle")
        cursor = orchestrator.get_or_create_cursor("run:cycle", scope=AutoScope.MILESTONE)
        decision, _, updated = orchestrator.tick(
            graph,
            cursor,
            {"T1": "NOT_STARTED", "T2": "NOT_STARTED"},
            {"G1": "NOT_REACHED"},
        )
        assert decision.action == ScopeAction.HALT_BLOCKED
        assert decision.reason_code == "AMBIGUOUS_PLAN_GRAPH"
        assert updated.status == "BLOCKED"
    finally:
        connection.close()


def test_milestone_gate_identity_is_canonical_end_to_end() -> None:
    graph = CanonicalPlanGraph(
        plan_identity="plan:gate-identity",
        plan_version=1,
        milestones=(
            PlanMilestoneNode("M1", "G1", task_ids=("T1",)),
            PlanMilestoneNode("M2", "G2", dependencies=("M1",), task_ids=("T2",)),
        ),
        tasks=(PlanTaskNode("T1", "M1"), PlanTaskNode("T2", "M2")),
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        orchestrator = ScopeOrchestrator(connection, "gate-identity")
        cursor = orchestrator.get_or_create_cursor(
            "run:gate-identity",
            scope=AutoScope.PROJECT,
        )
        decision, explanation, updated = orchestrator.tick(
            graph,
            cursor,
            {"T1": "ACCEPTED", "T2": "NOT_STARTED"},
            {"G1": "accepted", "G2": "NOT_REACHED"},
        )
        assert decision.action == ScopeAction.LAUNCH_TASK
        assert decision.selected_task_id == "T2"
        assert decision.selected_milestone_id == "M2"
        assert explanation.gate_evidence == {"G1": "ACCEPTED"}
        assert updated.last_accepted_gate == "G1"
        assert updated.current_milestone_id == "M2"
    finally:
        connection.close()


def test_until_stopped_round_trips_context_prerequisites_through_approved_graph() -> None:
    graph = CanonicalPlanGraph(
        plan_identity="plan:until-prerequisite",
        plan_version=1,
        milestones=(PlanMilestoneNode("M1", "GATE:M1", task_ids=("T0", "T1")),),
        tasks=(
            PlanTaskNode("T0", "M1"),
            PlanTaskNode("T1", "M1", dependencies=("G0",)),
        ),
        prerequisites=(PlanPrerequisiteNode("G0", "gate"),),
    )
    connection = sqlite3.connect(":memory:")
    controller = UntilStoppedController(connection, "until-prerequisite")
    controller.start(graph, run_id="run:until-prerequisite", explicit_scope=AutoScope.UNTIL_STOPPED)

    pending = controller.tick(
        None,
        {"T0": "ACCEPTED", "T1": "NOT_STARTED"},
        {"GATE:M1": "NOT_REACHED"},
        prerequisite_statuses={"G0": "pending"},
    )
    assert pending.decision.action == ScopeAction.WAIT_DEPENDENCY_PENDING

    runnable = controller.tick(
        None,
        {"T0": "ACCEPTED", "T1": "NOT_STARTED"},
        {"GATE:M1": "NOT_REACHED"},
        prerequisite_statuses={"G0": "passed"},
    )
    assert runnable.decision.action == ScopeAction.LAUNCH_TASK
    assert runnable.decision.selected_task_id == "T1"
