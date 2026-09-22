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
from types import SimpleNamespace
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


@pytest.mark.parametrize("delivery_failure", [False, True])
def test_real_gui_continue_uses_canonical_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivery_failure: bool) -> None:
    from test_project_execution_integration import _fixture, HEAD
    from bdb_vnext.project_workflow import CommandResult
    from bdb_vnext.resilient_project_workflow import ResilientProjectWorkflow
    from bdb_vnext.project_launch import ProjectLaunchQueueError
    app = _app()
    catalog, _, project_id = _fixture(tmp_path, all_deterministic=True)
    class HeadRunner:
        def run(self, args, **kwargs):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")
    workflow = ResilientProjectWorkflow(catalog.runtime_root, catalog=catalog, command_runner=HeadRunner())
    commands = CanonicalProjectCenterAutoCommands(
        catalog.runtime_root, project_id,
        project_provider=lambda: catalog.get(project_id),
        plan_provider=lambda: workflow.memory(project_id).current_plan(),
        memory_provider=lambda: workflow.memory(project_id),
    )
    commands.start_auto(AutoScope.MILESTONE, confirmed=True)
    active = ControlCenterSnapshot(
        runtime_root=str(catalog.runtime_root), system_state="ON", writer_state="ON",
        activation_state="ACTIVE", store_state="SEALED", store_instance_id="fixture",
        works=(), action_predicates=(), reason_code="production_acceptance_pass", read_only=False,
    )
    window = ProjectCenterWindow(
        runtime_root=catalog.runtime_root, catalog=catalog, workflow=workflow,
        snapshot_loader=lambda _: active, auto_commands_factory=lambda _: commands,
    )
    window.start_bootstrap()
    app.processEvents()
    if delivery_failure:
        def fail_write(*args, **kwargs):
            raise ProjectLaunchQueueError("queue_write_failed", "injected disk write failure")
        monkeypatch.setattr(workflow.queue, "_write_state_unlocked", fail_write)
    try:
        assert window._auto_continue_button.isEnabled()
        window._auto_continue_button.click()
        app.processEvents()
        if delivery_failure:
            assert "queue_write_failed" in window._status.text()
            assert "TASK_IN_PROGRESS" not in window._status.text()
            assert workflow.queue.peek() is None
            assert len(workflow.execution.pending_outbox_records(project_id)) == 1
        else:
            launch = workflow.queue.peek()
            assert launch is not None and launch.auto_send
            assert workflow.execution.snapshot(project_id)["current_binding_id"] == launch.execution_binding_id
            assert "QUEUED" in window._status.text()
            window._auto_continue_button.click()
            assert workflow.queue.peek().launch_id == launch.launch_id
            assert len(workflow.execution.snapshot(project_id)["bindings"]) == 1
    finally:
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
