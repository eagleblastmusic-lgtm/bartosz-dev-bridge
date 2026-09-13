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
from bdb_vnext.project_memory import ProjectMemoryStore
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
