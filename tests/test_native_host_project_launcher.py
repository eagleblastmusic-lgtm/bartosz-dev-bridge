from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.native_host import NATIVE_CONFIG_SCHEMA, NATIVE_REQUEST_SCHEMA, NativeArmStore, NativeHostConfig
from bdb_bridge.native_host_project_launcher import ProjectLauncherNativeHostService
from bdb_bridge.project_launch import ProjectLaunchQueue


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"


def native_config(tmp_path: Path) -> NativeHostConfig:
    control = tmp_path / "control"
    fixture = tmp_path / "fixture"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (control, fixture, worktrees, runtime):
        path.mkdir()
    bridge = tmp_path / "bridge.json"
    bridge.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(fixture),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "repository_id": "launcher-test",
                "allowed_paths": ["README.md"],
                "direct_spool_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "native-host.json"
    path.write_text(
        json.dumps(
            {
                "schema": NATIVE_CONFIG_SCHEMA,
                "repositories": {"alpha": {"bridge_config_path": str(bridge)}},
                "allowed_origins": [ORIGIN],
                "state_path": str(tmp_path / "native-host-arm.json"),
                "session_store_path": str(tmp_path / "native-host-sessions.json"),
                "max_wait_seconds": 1,
                "max_message_bytes": 65536,
            }
        ),
        encoding="utf-8",
    )
    return NativeHostConfig.from_json(path)


def request(action: str, **payload):
    return {
        "schema": NATIVE_REQUEST_SCHEMA,
        "request_id": f"request-{action}",
        "action": action,
        **payload,
    }


def test_peek_claim_and_acknowledge_project_launch(tmp_path: Path) -> None:
    config = native_config(tmp_path)
    NativeArmStore(config.state_path).arm(minutes=5)
    launch = ProjectLaunchQueue(tmp_path / "project-launch-queue.json").enqueue(
        repo_alias="alpha",
        prompt="Create a calculator",
        auto_send=True,
    )
    service = ProjectLauncherNativeHostService(config, origin=ORIGIN)
    claim_id = str(uuid.uuid4())

    peek = service.handle(request("project_launch_peek"))
    assert peek["status"] == "project_launch"
    assert peek["launch"]["launch_id"] == launch.launch_id

    claim = service.handle(
        request(
            "project_launch_claim",
            launch_id=launch.launch_id,
            claim_id=claim_id,
        )
    )
    assert claim["status"] == "claimed"
    assert claim["launch"]["prompt"] == "Create a calculator"

    competing = service.handle(
        request(
            "project_launch_claim",
            launch_id=launch.launch_id,
            claim_id=str(uuid.uuid4()),
        )
    )
    assert competing["status"] == "busy_or_missing"

    ack = service.handle(
        request(
            "project_launch_ack",
            launch_id=launch.launch_id,
            claim_id=claim_id,
        )
    )
    assert ack["status"] == "acknowledged"
    assert service.handle(request("project_launch_peek"))["status"] == "empty"


def test_project_launch_requires_armed_native_host(tmp_path: Path) -> None:
    config = native_config(tmp_path)
    service = ProjectLauncherNativeHostService(config, origin=ORIGIN)

    with pytest.raises(BridgeError) as error:
        service.handle(request("project_launch_peek"))

    assert error.value.code == "policy_denied"


def test_claim_rejects_invalid_uuid(tmp_path: Path) -> None:
    config = native_config(tmp_path)
    NativeArmStore(config.state_path).arm(minutes=5)
    service = ProjectLauncherNativeHostService(config, origin=ORIGIN)

    with pytest.raises(BridgeError) as error:
        service.handle(
            request(
                "project_launch_claim",
                launch_id="../escape",
                claim_id=str(uuid.uuid4()),
            )
        )

    assert error.value.code == "invalid_payload"
