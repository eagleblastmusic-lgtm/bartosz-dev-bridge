from __future__ import annotations

import io
import json
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.local_result_sink import LocalResultSink
from bdb_bridge.native_host import (
    NATIVE_CONFIG_SCHEMA,
    NATIVE_REQUEST_SCHEMA,
    NativeArmStore,
    NativeHostConfig,
    NativeHostService,
)
from bdb_bridge.native_messaging import encode_native_message, read_native_message
from bdb_bridge.protocol import result_path_for


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
NOW = datetime(2026, 7, 17, 3, 0, 0, tzinfo=timezone.utc)


def write_configs(tmp_path: Path, *, origins: list[str] | None = None) -> tuple[Path, Path]:
    control = tmp_path / "control"
    fixture = tmp_path / "fixture"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (control, fixture, worktrees, runtime):
        path.mkdir()
    bridge_config = tmp_path / "bridge.json"
    bridge_config.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(fixture),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "direct_spool_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    native_config = tmp_path / "native-host.json"
    native_config.write_text(
        json.dumps(
            {
                "schema": NATIVE_CONFIG_SCHEMA,
                "bridge_config_path": str(bridge_config),
                "allowed_origins": origins or [ORIGIN],
                "state_path": str(tmp_path / "native-host-arm.json"),
                "max_wait_seconds": 2,
                "max_message_bytes": 65536,
            }
        ),
        encoding="utf-8",
    )
    return bridge_config, native_config


def envelope() -> dict:
    return {
        "schema": "bdb-local-envelope-v1",
        "submitted_at": "2026-07-17T03:00:00Z",
        "manifest": {
            "schema_version": "1.1",
            "session_id": SESSION,
            "repository_id": "synthetic",
            "base_sha": "a" * 40,
            "created_at": "2026-07-17T03:00:00Z",
            "expires_at": "2026-07-17T03:05:00Z",
        },
        "command": {
            "schema_version": "1.1",
            "session_id": SESSION,
            "command_id": f"{SESSION}:000001",
            "sequence": 1,
            "operation": "open_read",
            "created_at": "2026-07-17T03:00:00Z",
            "expires_at": "2026-07-17T03:05:00Z",
            "expected_revision": 0,
            "expected_state_hash": None,
            "payload": {"path": "src/clamp.py"},
        },
    }


def test_native_message_round_trip_and_length_bounds() -> None:
    encoded = encode_native_message({"schema": "x", "value": "ą"})
    decoded = read_native_message(io.BytesIO(encoded))
    assert decoded == {"schema": "x", "value": "ą"}

    with pytest.raises(BridgeError):
        read_native_message(io.BytesIO(struct.pack("=I", 100) + b"{}"))
    with pytest.raises(BridgeError):
        read_native_message(io.BytesIO(struct.pack("=I", 2_000_000)), max_message_bytes=1024)


def test_native_config_requires_exact_origins_and_local_state(tmp_path: Path) -> None:
    _, config_path = write_configs(tmp_path)
    config = NativeHostConfig.from_json(config_path)
    assert config.allowed_origins == (ORIGIN,)

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["allowed_origins"] = ["chrome-extension://*/"]
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        NativeHostConfig.from_json(config_path)
    assert exc.value.code == "invalid_config"


def test_arm_store_expires_and_disarms_atomically(tmp_path: Path) -> None:
    now = [NOW]
    store = NativeArmStore(tmp_path / "arm.json", now_fn=lambda: now[0])

    armed = store.arm(minutes=5)
    assert armed.armed
    assert store.status().armed

    now[0] = datetime(2026, 7, 17, 3, 6, 0, tzinfo=timezone.utc)
    assert not store.status().armed
    assert not store.disarm().armed


def test_service_rejects_foreign_origin_and_disarmed_submit(tmp_path: Path) -> None:
    _, config_path = write_configs(tmp_path)
    config = NativeHostConfig.from_json(config_path)

    with pytest.raises(BridgeError) as foreign:
        NativeHostService(
            config,
            origin="chrome-extension://pppppppppppppppppppppppppppppppp/",
            now_fn=lambda: NOW,
        )
    assert foreign.value.code == "policy_denied"

    service = NativeHostService(config, origin=ORIGIN, now_fn=lambda: NOW)
    with pytest.raises(BridgeError) as disarmed:
        service.handle(
            {
                "schema": NATIVE_REQUEST_SCHEMA,
                "request_id": "request-1",
                "action": "submit",
                "filename": "action.json",
                "wait_seconds": 0,
                "envelope": envelope(),
            }
        )
    assert disarmed.value.code == "policy_denied"


def test_armed_submit_returns_existing_durable_result(tmp_path: Path) -> None:
    _, config_path = write_configs(tmp_path)
    config = NativeHostConfig.from_json(config_path)
    NativeArmStore(config.state_path, now_fn=lambda: NOW).arm(minutes=5)
    service = NativeHostService(config, origin=ORIGIN, now_fn=lambda: NOW)

    expected_result = {"status": "success", "end_marker": "BDB-END:sha256:test"}
    LocalResultSink(service.bridge_config.direct_result_dir).publish(
        result_path_for(SESSION, 1),
        json.dumps(expected_result).encode("utf-8"),
    )

    response = service.handle(
        {
            "schema": NATIVE_REQUEST_SCHEMA,
            "request_id": "request-2",
            "action": "submit",
            "filename": "action-000001.json",
            "wait_seconds": 0,
            "envelope": envelope(),
        }
    )

    assert response["status"] == "completed"
    assert response["command_id"] == f"{SESSION}:000001"
    assert response["result"] == expected_result
    assert response["arm"]["armed"] is True
    assert (service.bridge_config.direct_spool_dir / "action-000001.json").exists()


def test_result_poll_is_bounded_and_status_works_while_disarmed(tmp_path: Path) -> None:
    _, config_path = write_configs(tmp_path)
    config = NativeHostConfig.from_json(config_path)
    service = NativeHostService(config, origin=ORIGIN, now_fn=lambda: NOW)

    status = service.handle(
        {
            "schema": NATIVE_REQUEST_SCHEMA,
            "request_id": "status-1",
            "action": "status",
        }
    )
    assert status["status"] == "status"
    assert status["arm"]["armed"] is False

    NativeArmStore(config.state_path, now_fn=lambda: NOW).arm(minutes=5)
    pending = service.handle(
        {
            "schema": NATIVE_REQUEST_SCHEMA,
            "request_id": "result-1",
            "action": "result",
            "session_id": SESSION,
            "sequence": 1,
            "wait_seconds": 0,
        }
    )
    assert pending["status"] == "pending"
