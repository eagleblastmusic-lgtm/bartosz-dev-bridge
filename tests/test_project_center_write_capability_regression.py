"""Regression tests for BDB vNext write capability and fail-closed read-only projection.

Proves:
1. Canonical ACTIVE + production acceptance PASS -> Project Center starts writable,
   top bar does NOT say read-only, P3-03 Continue works, queue receives launch,
   Native Host sees launch.
2. Read-only mode -> all mutating buttons explicitly disabled with reasons,
   top bar contains read-only, mutating handler methods fail-closed.
3. Automated post-ACTIVE M9b reconciliation self-heals deployed M9b record to match
   active Bootstrap slot.
4. ResilientProjectWorkflow allows continue when milestone run is active without false
   disagreements with blocked retry evidence.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from bdb_vnext.composition import (
    BROWSER_EXTENSION_ID,
    GENERATION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
)
from bdb_vnext.control_center_query import (
    ActionPredicate,
    ControlCenterSnapshot,
    read_control_center_snapshot,
)
from bdb_vnext.control_store import CONTROL_DATABASE_RELATIVE_PATH, seal_path_for_database
from bdb_vnext.m9b_activation import ActivationRecord, read_activation, write_activation
from bdb_vnext.m9b_native_host import (
    M9B_NATIVE_REQUEST_SCHEMA,
    VNextNativeConfig,
    handle_message,
)
from bdb_vnext.m9b_reconciliation import (
    ensure_post_active_m9b_reconciled,
    query_post_active_reconciliation,
)
from bdb_vnext.project_catalog import (
    ProjectBrief,
    ProjectCatalog,
    ProjectRecord,
    new_project_record,
)
from bdb_vnext.project_center_auto import (
    AutoScope,
    CanonicalAutoState,
    CanonicalProjectCenterAutoCommands,
    DEFAULT_AUTO_SCOPE,
    ProjectCenterAutoViewModel,
)
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter
from bdb_gui.project_center import ProjectCenterWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication(["test-write-capability-regression"])


def _fake_project(project_id: str = "premium-calculator", repo_dir: Path | None = None) -> ProjectRecord:
    rec = new_project_record(
        project_id=project_id,
        display_name="Premium Calculator",
        repo_alias="premium-calculator",
        local_repo_path=repo_dir or Path("C:/Projekty/DevMaster/premium-calculator"),
        github_repo=None,
        brief=ProjectBrief(
            name="Premium Calculator",
            goal="Kalkulator składek ubezpieczeniowych",
            description="Wyliczenia zgodne ze specyfikacją",
            project_type="tool",
        ),
    )
    return replace(
        rec,
        plan_imported=True,
        plan_version="1",
        total_tasks=10,
        completed_tasks=3,
        current_milestone="P3",
        current_task="P3-03",
        project_status="active",
    )


def test_read_only_mode_disables_all_mutating_buttons_and_guards_execution(tmp_path: Path) -> None:
    app = _app()
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    catalog = ProjectCatalog(runtime)
    project = _fake_project()
    catalog.upsert(project)

    read_only_snapshot = ControlCenterSnapshot(
        runtime_root=str(runtime),
        system_state="OFF",
        writer_state="OFF",
        activation_state="OFF",
        store_state="SEALED",
        store_instance_id="store-1",
        works=(),
        action_predicates=(ActionPredicate("continue", enabled=False),),
        reason_code="authority_unavailable",
        read_only=True,
    )

    window = ProjectCenterWindow(
        runtime_root=runtime,
        catalog=catalog,
        snapshot_loader=lambda _root: read_only_snapshot,
    )
    window.start_bootstrap()
    app.processEvents()

    # 1. Top bar must indicate read-only
    assert "read-only" in window._status.text()
    assert window._is_read_only() is True

    # 2. Mutating buttons in Current Project page must be disabled
    assert window._import_plan_button.isEnabled() is False
    assert window._plan_prompt_button.isEnabled() is False
    assert window._work_prompt_button.isEnabled() is False
    assert window._start_button.isEnabled() is False
    assert window._continue_button.isEnabled() is False
    assert window._handoff_button.isEnabled() is False
    assert window._approve_review_button.isEnabled() is False
    assert window._changes_review_button.isEnabled() is False
    assert window._project_review_button.isEnabled() is False

    # Check tooltips reflect read-only
    assert "read-only" in window._continue_button.toolTip()
    assert "read-only" in window._new_button.toolTip()
    assert "read-only" in window._open_button.toolTip()

    # 3. AUTO page mutating buttons must be disabled
    assert window._auto_start_button.isEnabled() is False
    assert window._auto_continue_button.isEnabled() is False
    assert window._auto_resume_button.isEnabled() is False
    assert window._auto_stop_button.isEnabled() is False
    assert window._auto_milestone_gate_button.isEnabled() is False
    assert window._auto_planning_gate_button.isEnabled() is False
    assert window._auto_open_question_button.isEnabled() is False
    assert "read-only" in window._auto_disabled_reason.text()

    # 4. Environment page mutating buttons must be disabled
    assert window._env_prepare_button.isEnabled() is False
    assert "read-only" in window._env_prepare_button.toolTip()

    # 5. Mutating handlers must fail-closed if invoked directly
    window._continue_auto_from_gui()
    assert "odrzucono operację" in window._status.text()

    window._start_auto_from_gui()
    assert "odrzucono operację" in window._status.text()

    window._prepare_environment_from_gui()
    assert "odrzucono operację" in window._status.text()

    window._new_project()
    assert "odrzucono operację" in window._status.text()

    window._import_plan()
    assert "odrzucono operację" in window._status.text()

    window.close()


def test_canonical_active_makes_project_center_writable_and_continue_queues_launch(tmp_path: Path) -> None:
    app = _app()
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    catalog = ProjectCatalog(runtime)
    project = _fake_project()
    catalog.upsert(project)

    active_snapshot = ControlCenterSnapshot(
        runtime_root=str(runtime),
        system_state="ON",
        writer_state="ON",
        activation_state="ACTIVE",
        store_state="SEALED",
        store_instance_id="store-active",
        works=(),
        action_predicates=(
            ActionPredicate("resume", enabled=True),
            ActionPredicate("apply_effect", enabled=True),
            ActionPredicate("publish", enabled=True),
            ActionPredicate("activate", enabled=True),
        ),
        reason_code="production_acceptance_pass",
        read_only=False,
    )

    queue_path = runtime / "control" / "project-launch-queue.json"
    queue = ProjectLaunchQueueAdapter(queue_path)

    class FakeWorkflow:
        def __init__(self, rt: Path, cat: ProjectCatalog) -> None:
            self.runtime_root = rt
            self.catalog = cat
            self.queue = queue
            self.execution = MagicMock()
            self.memory = MagicMock()

        def queue_continue_prompt(self, project_id: str) -> Any:
            launch = queue.enqueue(
                repo_alias="premium-calculator",
                prompt=f"Continue prompt for {project_id} / P3-03",
                project_id=project_id,
                task_id="P3-03",
            )
            return launch

    workflow = FakeWorkflow(runtime, catalog)

    auto_state = CanonicalAutoState(
        project_id=project.project_id,
        scope=AutoScope.MILESTONE,
        current_milestone_id="P3",
        current_task_id="P3-03",
        scope_status="ACTIVE",
        continuation_status="CONTINUE_AVAILABLE",
        reentry_status="NONE",
        reason="ACTIVE",
        plan_available=True,
        p2_completed=True,
        p3_started=True,
    )
    commands = MagicMock()
    commands.snapshot.return_value = auto_state

    receipt = MagicMock()
    receipt.reason_code = "TASK_IN_PROGRESS"
    receipt.explanation = "Current task P3-03 is in progress."
    receipt.current_milestone_id = "P3"
    receipt.current_task_id = "P3-03"

    def continue_auto() -> Any:
        workflow.queue_continue_prompt(project.project_id)
        return receipt

    commands.continue_auto.side_effect = continue_auto

    window = ProjectCenterWindow(
        runtime_root=runtime,
        catalog=catalog,
        workflow=workflow,
        snapshot_loader=lambda _root: active_snapshot,
        auto_commands_factory=lambda _proj: commands,
    )
    window.start_bootstrap()
    app.processEvents()

    # 1. Top bar must NOT say read-only
    status_text = window._status.text()
    assert "BDB: ON" in status_text
    assert "read-only" not in status_text
    assert window._is_read_only() is False

    # 2. Mutating buttons in AUTO must be active
    assert window._auto_continue_button.isEnabled() is True

    # 3. Click "Kontynuuj" in AUTO
    window._continue_auto_from_gui()
    app.processEvents()

    # 4. Verify launch was queued in project-launch-queue.json
    pending_launch = queue.peek()
    assert pending_launch is not None
    assert pending_launch.project_id == project.project_id
    assert pending_launch.task_id == "P3-03"
    assert "Continue prompt" in pending_launch.prompt

    # 5. Verify Native Host sees the launch via project_launch_peek
    config_doc = {
        "schema": "bdb-vnext-native-host-config-v2",
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "runtime_root": str(runtime),
        "legacy_runtime_root": str(tmp_path / "legacy"),
        "bootstrap_authority_root": str(tmp_path / "bootstrap"),
    }
    (tmp_path / "legacy").mkdir()
    (tmp_path / "bootstrap").mkdir()
    config_path = runtime / "config" / "native-host.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config_doc), encoding="utf-8")

    native_config = VNextNativeConfig.from_json(config_path)
    response = handle_message(
        native_config,
        {
            "schema": M9B_NATIVE_REQUEST_SCHEMA,
            "request_id": "test-req-1",
            "action": "project_launch_peek",
            "protocol_generation": PROTOCOL_GENERATION,
        },
    )
    assert response["status"] == "project_launch"
    assert response["launch"]["project_id"] == project.project_id
    assert response["launch"]["task_id"] == "P3-03"

    window.close()


def test_ensure_post_active_m9b_reconciled_self_heals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority = tmp_path / "authority"
    deployed = tmp_path / "deployed"
    client = tmp_path / "client"
    for path in (authority, deployed, client):
        path.mkdir(parents=True, exist_ok=True)

    old_head = "c" * 40
    new_head = "a" * 40
    old_record = ActivationRecord(
        activation_id="m9b-old-reconciliation",
        state="ACTIVE",
        source_head=old_head,
        source_tree="d" * 40,
        m9a_freeze_digest="sha256:" + "1" * 64,
        browser_bundle_digest="sha256:" + "7" * 64,
        native_manifest_digest="sha256:" + "8" * 64,
        writer_enabled=True,
        intake_enabled=True,
    )
    write_activation(deployed, old_record)

    maint_id = "m11c-test-reconciliation"
    maint_sha = "sha256:" + "4" * 64
    route_sha = "sha256:" + "5" * 64
    boot_sha = "sha256:" + "1" * 64
    client_sha = "sha256:" + "6" * 64

    subject = {
        "maintenance": {
            "plan_sha256": maint_sha,
            "maintenance_id": maint_id,
            "route_transition_plan_sha256": route_sha,
            "candidate_source_head": new_head,
            "candidate_source_tree": "b" * 40,
            "candidate_manifest_sha256": "sha256:" + "a" * 64,
            "candidate_bundle_sha256": "sha256:" + "9" * 64,
            "client_plan_sha256": client_sha,
            "candidate_native_manifest_path": "C:/candidate/native-host.json",
            "browser_bundle_digest": "sha256:" + "7" * 64,
            "native_manifest_digest": "sha256:" + "8" * 64,
            "canonical_runtime_root": str(client),
        },
        "route_plan": {"route_transition_plan_sha256": route_sha},
        "route_state": {"phase": "COMPLETED", "bootstrap_phase": "NEW", "bootstrap_state_sha256": boot_sha},
        "bootstrap": {"state_sha256": boot_sha, "active_manifest_sha256": "sha256:" + "2" * 64, "previous_manifest_sha256": "sha256:" + "3" * 64},
        "active": {"source_commit": new_head, "source_tree": "b" * 40},
        "client_plan": {"client_plan_sha256": client_sha, "browser_bundle_digest": "sha256:" + "7" * 64, "native_manifest_sha256": "sha256:" + "8" * 64},
        "client_verification": {"verification_sha256": client_sha},
        "routes": {"target_registered": True, "target_conflict": False, "legacy_route_present": False},
        "m9b": old_record,
        "m3c": {"control_digest": client_sha, "kill_switch_digest": client_sha},
    }

    import bdb_vnext.m9b_reconciliation as m9b_rec
    monkeypatch.setattr(m9b_rec, "_subject", lambda **_: subject)
    monkeypatch.setattr(
        m9b_rec,
        "observe_bootstrap_activation",
        lambda **_: {
            "status": "ACTIVE",
            "state": {"activation_id": f"m11c-maint-{maint_id}", "state_sha256": boot_sha},
            "slots": {"ACTIVE": {"source_commit": new_head}},
        },
    )
    monkeypatch.setattr(
        m9b_rec,
        "query_post_active_maintenance",
        lambda **_: {
            "plan": subject["maintenance"],
            "route_transition_plan": subject["route_plan"],
            "route_transition_state": subject["route_state"],
        },
    )

    res = ensure_post_active_m9b_reconciled(
        authority_root=authority,
        deployed_runtime_root=deployed,
        maintenance_id=maint_id,
    )
    assert res["status"] == "COMPLETED"
    assert res["target_matches"] is True

    reconciled = read_activation(deployed)
    assert reconciled is not None
    assert reconciled.source_head == new_head


def test_resilient_project_workflow_allows_continue_when_run_is_active(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from bdb_vnext.resilient_project_workflow import ResilientProjectWorkflow

    class _Probe(ResilientProjectWorkflow):
        def __init__(self, execution_state: dict[str, Any]) -> None:
            self.project = SimpleNamespace(
                project_id="test-proj-running",
                repo_alias="test-proj",
                local_repo_path=str(tmp_path / "repo"),
                github_repo=None,
            )
            self.plan = SimpleNamespace(
                plan_version="1",
                tasks=[SimpleNamespace(task_id="P3-03", milestone_id="P3", status="active")],
            )
            self.catalog = MagicMock()
            self.catalog.get.return_value = self.project
            self.execution = MagicMock()
            self._mem = MagicMock()
            self._mem.current_plan.return_value = self.plan
            self._mem.read_state.return_value = SimpleNamespace(execution=execution_state)
            self.queued: Any = None

        def memory(self, _project_id: str) -> Any:
            return self._mem

        def current_repo_head(self, _project: Any) -> str:
            return "a" * 40

        def _queue_execution_prompt(self, project_id: str, kind: str, *, binding_override: Any = None) -> Any:
            self.queued = (project_id, kind, binding_override)
            return "queued-launch-ok"

    running_state = {
        "current_task_id": "P3-03",
        "task_statuses": {"P3-03": "active"},
        "active_milestone_run": {
            "milestone_run_id": "run-p3",
            "milestone_id": "P3",
            "status": "running",
            "current_task_id": "P3-03",
        },
        "attempts": [],
    }
    workflow = _Probe(running_state)
    result = workflow.queue_continue_prompt("test-proj-running")
    assert result == "queued-launch-ok"
    assert workflow.queued == ("test-proj-running", "continue", None)
