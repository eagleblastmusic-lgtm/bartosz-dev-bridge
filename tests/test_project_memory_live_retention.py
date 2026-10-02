from dataclasses import replace
import json
import sqlite3

import pytest

from bdb_vnext.project_catalog import ProjectBrief, ProjectCatalog, new_project_record, validate_project_plan
from bdb_vnext.project_execution import ProjectExecutionCoordinator
from bdb_vnext.project_memory import ProjectMemoryError, ProjectMemoryStore
from bdb_vnext.project_workflow import ProjectWorkflow


def populate_events(store, count):
    def transition(state):
        for _ in range(count):
            state = store._append_event(state, "EXECUTION_CHECKPOINT", "Durable audit history")
        return state, None
    store.write_transaction(transition)


def test_actual_v1_workflow_exceeds_events_bindings_attempts_and_receipts(tmp_path):
    runtime, repo, project_id = tmp_path / "runtime", tmp_path / "repo", "growth-project"
    (repo / ".git").mkdir(parents=True)
    catalog = ProjectCatalog(runtime)
    project = new_project_record(project_id=project_id, display_name="Growth", repo_alias="growth", local_repo_path=repo, github_repo=None, brief=ProjectBrief("Growth", "goal", "description", "tool"))
    catalog.upsert(project)
    store = ProjectMemoryStore(runtime, project_id)
    tasks = [{"id": f"t{i}", "milestone_id": "m1", "title": f"Task {i}", "description": "Check fixture", "status": "pending", "dependencies": [], "acceptance_criteria": ["test:fixture"]} for i in range(514)]
    plan = validate_project_plan({"schema": "bdb-project-plan-v1", "project_id": project_id, "project_name": "Growth", "plan_version": "1", "milestones": [{"id": "m1", "title": "Growth", "description": "Growth", "status": "active"}], "tasks": tasks, "current_task_id": "t0"})
    store.ensure_initial_plan(plan)
    catalog.upsert(replace(project, plan_imported=True, plan_version="1", total_tasks=len(tasks), plan_path=str(store.current_pointer), current_task="t0"))
    populate_events(store, 2048)
    workflow = ProjectWorkflow(runtime, catalog=catalog)
    first_result = None
    first_attempt = None
    for i in range(514):
        binding = workflow.execution.start(project_id, task_id=f"t{i}", expected_repo_head_before="a" * 40)
        result = {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id,
                  "head_before": "a" * 40, "head_after": "a" * 40, "execution_status": "PASS", "validation_status": "PASS", "promotion_status": "NOT_RUN",
                  "result_summary": "Fixture passed", "evidence_refs": ["test:fixture"]}
        attempt = workflow.execution.record_result(project_id, result)
        assert attempt.result_status == "PASS"
        if first_result is None: first_result, first_attempt = result, attempt
    physical = json.loads(store.memory_path.read_bytes())
    assert len(physical["events"]) <= 2048
    assert len(physical["execution"]["bindings"]) <= 512
    assert len(physical["execution"]["attempts"]) <= 512
    reopened = ProjectExecutionCoordinator(str(runtime), catalog=ProjectCatalog(runtime))
    assert reopened.record_result(project_id, first_result).attempt_id == first_attempt.attempt_id
    state = ProjectMemoryStore(runtime, project_id).read_state()
    assert len(state.events) > 2048 and len(state.execution["attempts"]) == 514
    restored = ProjectMemoryStore(tmp_path / "restored", project_id)
    restored.restore_archive(store.export_archive())
    assert restored.read_state() == state
    restored.append_event("MILESTONE_AUTO_STOPPED", "STOP after archive restore", milestone_id="m1")
    assert restored.read_state().events[-1].event_type == "MILESTONE_AUTO_STOPPED"


def test_compaction_crash_preserves_old_authority_and_exact_recovery(tmp_path, monkeypatch):
    from bdb_vnext import project_memory as memory_module
    store = ProjectMemoryStore(tmp_path, "p1")
    populate_events(store, 2047)
    before = store.memory_path.read_bytes()
    original = memory_module._atomic_write
    def fail_replace(path, document):
        if path == store.memory_path: raise OSError("Crash after durable CAS before authority replace")
        return original(path, document)
    monkeypatch.setattr(memory_module, "_atomic_write", fail_replace)
    with pytest.raises(OSError): store.append_event("MILESTONE_AUTO_STOPPED", "STOP")
    assert store.memory_path.read_bytes() == before
    assert ProjectMemoryStore(tmp_path, "p1").read_state().events[-1].event_id == "p1:e002047"
    monkeypatch.setattr(memory_module, "_atomic_write", original)
    event = store.append_event("MILESTONE_AUTO_STOPPED", "STOP")
    assert event.event_id == "p1:e002048"
    assert store.read_state().events[-1] == event


def test_missing_or_tampered_retention_is_typed_without_repair(tmp_path):
    store = ProjectMemoryStore(tmp_path, "p1")
    populate_events(store, 2048)
    before = store.memory_path.read_bytes()
    conn = sqlite3.connect(store.root / "retention.db")
    conn.execute("UPDATE content_blobs SET payload = 'tampered'")
    conn.commit(); conn.close()
    with pytest.raises(ProjectMemoryError) as caught: store.read_state()
    assert caught.value.code == "memory_retention_invalid"
    assert store.memory_path.read_bytes() == before


def test_unresolved_execution_is_never_evicted_for_capacity(tmp_path):
    store = ProjectMemoryStore(tmp_path, "p1")
    store.append_event("PROJECT_CREATED", "Created")
    before = store.memory_path.read_bytes()
    with pytest.raises(ProjectMemoryError):
        store.write_transaction(lambda state: (replace(state, execution={"bindings": [{"execution_binding_id": str(i), "status": "ACTIVE"} for i in range(513)]}), None))
    assert store.memory_path.read_bytes() == before


def test_retained_authority_is_not_silently_imported_as_only_a_tail(tmp_path):
    from bdb_vnext.v1_v2_shadow_migration import discover_v1_inventory, V1BackupService
    store = ProjectMemoryStore(tmp_path / "runtime", "p1")
    populate_events(store, 2048)
    before = store.memory_path.read_bytes()
    inventory = discover_v1_inventory(store.memory_path)
    assert not inventory.is_valid and inventory.is_unsupported_version
    assert "export_archive" in inventory.error_message
    with pytest.raises(ValueError): V1BackupService(tmp_path / "backup").create_backup(store.memory_path, inventory)
    logical = store.export_archive()["document"]
    assert discover_v1_inventory(logical).record_counts["events"] == 2048
    assert store.memory_path.read_bytes() == before


def test_v1_stop_projection_failure_preserves_durable_v2_stop_fence(tmp_path, monkeypatch):
    from bdb_vnext.project_catalog import ProjectPlan, ProjectMilestone, ProjectTask
    from bdb_vnext.project_center_auto import CanonicalProjectCenterAutoCommands, AutoScope
    plan = ProjectPlan("p1", "STOP", "1", (ProjectMilestone("m1", "One", "One", "active"),), (ProjectTask("t1", "m1", "One", "One", "pending"),), "t1")
    store = ProjectMemoryStore(tmp_path, "p1")
    store.ensure_initial_plan(plan)
    project = new_project_record(project_id="p1", display_name="STOP", repo_alias="stop", local_repo_path=tmp_path / "repo", github_repo=None, brief=ProjectBrief("STOP", "goal", "description", "tool"))
    commands = CanonicalProjectCenterAutoCommands(tmp_path, "p1", project_provider=lambda: project, plan_provider=lambda: plan, memory_provider=lambda: store)
    commands.start_auto(AutoScope.MILESTONE, confirmed=True)
    store.write_transaction(lambda state: (replace(state, execution={**state.execution, "bindings": [{"execution_binding_id": str(i), "status": "ACTIVE"} for i in range(512)]}), None))
    assert store.capacity_status()["status"] == "CAPACITY_WARNING"
    before = store.memory_path.read_bytes()
    receipt = commands.stop_auto()
    assert receipt.reason_code == "STOPPED"
    def fail(*args): raise OSError("Injected projection disk failure")
    monkeypatch.setattr(store, "_write_state", fail)
    with pytest.raises(OSError): store.append_event("MILESTONE_AUTO_STOPPED", "STOP projection")
    assert store.memory_path.read_bytes() == before
    assert CanonicalProjectCenterAutoCommands(tmp_path, "p1", plan_provider=lambda: plan).snapshot(plan_available=True).stop_fenced


def test_committed_pointer_restart_and_tampered_export_preserve_identity(tmp_path):
    store = ProjectMemoryStore(tmp_path, "p1")
    populate_events(store, 2048)
    expected = store.read_state()
    assert ProjectMemoryStore(tmp_path, "p1").read_state() == expected
    export = store.export_archive()
    export["document"]["events"][0]["human_summary"] = "tampered"
    target = ProjectMemoryStore(tmp_path / "restore", "p1")
    with pytest.raises(ProjectMemoryError) as caught: target.restore_archive(export)
    assert caught.value.code == "memory_archive_invalid" and not target.memory_path.exists()
