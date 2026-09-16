from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.native_host import (
    NATIVE_CONFIG_SCHEMA,
    NATIVE_REQUEST_SCHEMA,
    NativeArmStore,
    NativeHostConfig,
)
from bdb_bridge.native_host_project_launcher import ProjectLauncherNativeHostService
from bdb_vnext.project_launch import ProjectLaunchQueueAdapter


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
CONVERSATION_ID = "conversation-0001"


def native_config(tmp_path: Path) -> NativeHostConfig:
    control_repo = tmp_path / "repo-control"
    fixture = tmp_path / "fixture"
    worktrees = tmp_path / "worktrees"
    bridge_runtime = tmp_path / "bridge-runtime"
    native_control = tmp_path / "runtime" / "control"
    for path in (control_repo, fixture, worktrees, bridge_runtime, native_control):
        path.mkdir(parents=True, exist_ok=True)
    bridge = tmp_path / "bridge.json"
    bridge.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control_repo),
                "fixture_repo_path": str(fixture),
                "worktree_root": str(worktrees),
                "runtime_dir": str(bridge_runtime),
                "repository_id": "launcher-test",
                "allowed_paths": ["README.md"],
                "direct_spool_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    config_path = native_control / "native-host.json"
    config_path.write_text(
        json.dumps(
            {
                "schema": NATIVE_CONFIG_SCHEMA,
                "repositories": {"alpha": {"bridge_config_path": str(bridge)}},
                "allowed_origins": [ORIGIN],
                "state_path": str(native_control / "native-host-arm.json"),
                "session_store_path": str(native_control / "native-host-sessions.json"),
                "max_wait_seconds": 1,
                "max_message_bytes": 65536,
            }
        ),
        encoding="utf-8",
    )
    return NativeHostConfig.from_json(config_path)


def request(action: str, **payload):
    return {
        "schema": NATIVE_REQUEST_SCHEMA,
        "request_id": f"request-{action}",
        "action": action,
        **payload,
    }


class _Canonical:
    def __init__(self) -> None:
        self.acknowledged = False
        self.activated: list[str] = []
        self.acks: list[tuple[str, str]] = []

    def is_canonical_launch(self, launch) -> bool:
        return launch.project_id is not None

    def is_acknowledged(self, launch) -> bool:
        return self.acknowledged

    def activate_claimed(self, launch) -> None:
        self.activated.append(launch.launch_id)

    def acknowledge_delivery(self, launch, conversation_id: str) -> None:
        self.acks.append((launch.launch_id, conversation_id))
        self.acknowledged = True


def enqueue_rich(config: NativeHostConfig):
    queue = ProjectLaunchQueueAdapter(config.state_path.parent / "project-launch-queue.json")
    return queue, queue.enqueue(
        repo_alias="alpha",
        prompt="Continue canonical task",
        auto_send=False,
        launch_id=str(uuid.uuid4()),
        project_id="project-0001",
        plan_version="1",
        task_id="P3-02",
        execution_binding_id="binding-0001",
        correlation_id="corr-0001",
        command_id="command-0001",
        expected_repo_head_before="7" * 40,
    )


def test_rich_ack_requires_conversation_and_keeps_queue_on_rejection(tmp_path: Path) -> None:
    config = native_config(tmp_path)
    NativeArmStore(config.state_path).arm(minutes=5)
    queue, launch = enqueue_rich(config)
    canonical = _Canonical()
    service = ProjectLauncherNativeHostService(
        config,
        origin=ORIGIN,
        canonical_state=canonical,
    )
    claim_id = str(uuid.uuid4())

    claim = service.handle(
        request("project_launch_claim", launch_id=launch.launch_id, claim_id=claim_id)
    )
    assert claim["status"] == "claimed"
    assert canonical.activated == [launch.launch_id]

    with pytest.raises(BridgeError) as exc:
        service.handle(
            request("project_launch_ack", launch_id=launch.launch_id, claim_id=claim_id)
        )

    assert exc.value.code == "invalid_payload"
    assert queue.peek() is not None
    assert canonical.acks == []


def test_rich_ack_is_canonical_before_transport_projection_disappears(tmp_path: Path) -> None:
    config = native_config(tmp_path)
    NativeArmStore(config.state_path).arm(minutes=5)
    queue, launch = enqueue_rich(config)
    canonical = _Canonical()
    service = ProjectLauncherNativeHostService(
        config,
        origin=ORIGIN,
        canonical_state=canonical,
    )
    claim_id = str(uuid.uuid4())
    service.handle(
        request("project_launch_claim", launch_id=launch.launch_id, claim_id=claim_id)
    )

    ack = service.handle(
        request(
            "project_launch_ack",
            launch_id=launch.launch_id,
            claim_id=claim_id,
            conversation_id=CONVERSATION_ID,
        )
    )

    assert ack["status"] == "acknowledged"
    assert canonical.acks == [(launch.launch_id, CONVERSATION_ID)]
    assert canonical.acknowledged is True
    assert queue.peek() is None


def test_stale_queue_projection_is_consumed_without_redelivering_acknowledged_launch(tmp_path: Path) -> None:
    config = native_config(tmp_path)
    NativeArmStore(config.state_path).arm(minutes=5)
    queue, launch = enqueue_rich(config)
    canonical = _Canonical()
    canonical.acknowledged = True
    service = ProjectLauncherNativeHostService(
        config,
        origin=ORIGIN,
        canonical_state=canonical,
    )
    claim_id = str(uuid.uuid4())

    claim = service.handle(
        request("project_launch_claim", launch_id=launch.launch_id, claim_id=claim_id)
    )

    assert claim["status"] == "already_acknowledged"
    assert claim["launch"] is None
    assert canonical.activated == []
    assert queue.peek() is None
