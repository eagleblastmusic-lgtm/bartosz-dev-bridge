"""Real framed stdio and restarted Native processes; not a Chrome E2E claim."""
import json
import struct
import subprocess
import sys
import uuid

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import VNextNativeConfig, M9B_NATIVE_REQUEST_SCHEMA
from bdb_vnext.project_workflow import ProjectWorkflow, CommandResult
from test_project_execution_integration import _fixture, HEAD


def test_native_process_lease_ownership_send_confirmation_and_restart(tmp_path):
    catalog, _, project_id = _fixture(tmp_path, all_deterministic=True)
    class HeadRunner:
        def run(self, args, **kwargs):
            return CommandResult(tuple(args), 0, HEAD + "\n", "")
    workflow = ProjectWorkflow(catalog.runtime_root, command_runner=HeadRunner())
    workflow.execution.begin_milestone_auto(project_id, milestone_id="m1")
    launch, _ = workflow.ensure_auto_current_launch(project_id)
    from bdb_vnext.project_center_auto import CanonicalProjectCenterAutoCommands, AutoScope
    commands = CanonicalProjectCenterAutoCommands(
        catalog.runtime_root, project_id, plan_provider=lambda: workflow.memory(project_id).current_plan(),
        project_provider=lambda: catalog.get(project_id),
        memory_provider=lambda: workflow.memory(project_id),
    )
    commands.start_auto(AutoScope.MILESTONE, confirmed=True)
    config = VNextNativeConfig(runtime_root=catalog.runtime_root, legacy_runtime_root=tmp_path / "legacy", bootstrap_authority_root=tmp_path / "bootstrap")
    config_path = tmp_path / "native.json"
    config_path.write_text(json.dumps(config.as_dict()), encoding="utf-8")
    def request(action, **extra):
        message = {
            "schema": M9B_NATIVE_REQUEST_SCHEMA, "request_id": str(uuid.uuid4()),
            "action": action, "protocol_generation": PROTOCOL_GENERATION,
            "browser_extension_id": BROWSER_EXTENSION_ID, **extra,
        }
        payload = json.dumps(message).encode()
        process = subprocess.run(
            [sys.executable, "-m", "bdb_vnext.m9b_native_host", f"chrome-extension://{BROWSER_EXTENSION_ID}/", "--config", str(config_path)],
            input=struct.pack("<I", len(payload)) + payload, capture_output=True, timeout=30,
        )
        assert process.returncode == 0, process.stderr.decode(errors="replace")
        size = struct.unpack("<I", process.stdout[:4])[0]
        assert size == len(process.stdout) - 4
        return json.loads(process.stdout[4:])

    owner = str(uuid.uuid4())
    rival = str(uuid.uuid4())
    # A valid identity must not authorize tampered transport prompt bytes.
    queue_bytes = workflow.queue.path.read_bytes()
    document = json.loads(queue_bytes)
    document["pending"]["prompt"] = "foreign prompt"
    workflow.queue.path.write_text(json.dumps(document), encoding="utf-8")
    rejected = request("project_launch_claim", launch_id=launch.launch_id, claim_id=owner, conversation_id="conversation-owner")
    assert rejected["error_code"] == "project_launch_payload_mismatch"
    assert workflow.execution.binding(project_id, launch.execution_binding_id).conversation_id is None
    workflow.queue.path.write_bytes(queue_bytes)
    # A pre-existing transport lease must prevent a losing claimant binding the conversation.
    workflow.queue.claim(launch_id=launch.launch_id, claim_id=owner)
    denied = request("project_launch_claim", launch_id=launch.launch_id, claim_id=rival, conversation_id="conversation-rival")
    assert denied["status"] == "busy_or_missing"
    assert workflow.execution.binding(project_id, launch.execution_binding_id).conversation_id is None
    claimed = request("project_launch_claim", launch_id=launch.launch_id, claim_id=owner, conversation_id="conversation-owner")
    assert claimed["status"] == "claimed"
    rejected = request("project_launch_claim", launch_id=launch.launch_id, claim_id=rival, conversation_id="conversation-rival")
    assert rejected["error_code"] == "execution_conversation_mismatch"
    ack = dict(launch_id=launch.launch_id, claim_id=owner, conversation_id="conversation-owner")
    rejected = request("project_launch_ack", **ack)
    assert rejected["error_code"] == "launch_send_confirmation_required"
    assert workflow.queue.peek() is not None
    assert workflow.execution.launch_handoff(project_id, launch.execution_binding_id)["status"] == "PENDING"
    confirmed = dict(**ack, project_id=project_id, execution_binding_id=launch.execution_binding_id, handoff_status="SENT")
    # Simulate GUI stopping after durable v2 STOP, before updating v1 run.
    commands.stop_auto()
    assert workflow.memory(project_id).read_state().execution["active_milestone_run"]["status"] == "running"
    status = request("project_execution_status", project_id=project_id, execution_binding_id=launch.execution_binding_id, conversation_id="conversation-owner")
    assert status["milestone_auto"]["status"] == "STOPPED"
    assert workflow.ensure_auto_current_launch(project_id) == (None, "not_runnable")
    # A completed physical send can still be acknowledged after STOP, but no
    # following task is dispatched. STOP is not permission to lose delivery evidence.
    assert request("project_launch_ack", **confirmed)["status"] == "acknowledged"
    assert workflow.execution.launch_handoff(project_id, launch.execution_binding_id)["status"] == "SENT"
    assert workflow.queue.peek() is None
    assert request("project_launch_ack", **confirmed)["status"] == "acknowledged"
    assert request("project_launch_peek")["status"] == "empty"
    commands.resume_auto()
    assert workflow.ensure_auto_current_launch(project_id) == (None, "already_sent")
    assert workflow.execution.snapshot(project_id)["attempts"] == []
