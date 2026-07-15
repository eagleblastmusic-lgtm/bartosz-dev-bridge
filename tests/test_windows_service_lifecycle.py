from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest

from bdb_bridge import Journal, ServiceStatusReader, ServiceStatus, InstanceLock, BridgeConfig, is_pid_alive

# Skip entire file on non-Windows platforms
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows background service mode is only supported on Windows")


@pytest.fixture
def make_config_file(tmp_path: Path):
    def _make():
        control = tmp_path / "control"
        control.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=control, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=control)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=control)

        (control / "dummy.txt").write_text("initial")
        subprocess.run(["git", "add", "dummy.txt"], cwd=control)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=control)

        subprocess.run(["git", "checkout", "-b", "commands"], cwd=control, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "checkout", "-b", "results"], cwd=control, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "checkout", "main"], cwd=control, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cfg = BridgeConfig(
            control_repo_path=control,
            fixture_repo_path=tmp_path / "fixture",
            worktree_root=tmp_path / "worktrees",
            runtime_dir=tmp_path / "runtime",
            heartbeat_stale_seconds=5.0,
            heartbeat_interval_seconds=1.0,
            idle_poll_seconds=1.0,
        )
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)

        cfg_dict = {
            "schema_version": "1.1",
            "control_repo_path": str(control),
            "fixture_repo_path": str(tmp_path / "fixture"),
            "worktree_root": str(tmp_path / "worktrees"),
            "runtime_dir": str(tmp_path / "runtime"),
            "heartbeat_stale_seconds": 5.0,
            "heartbeat_interval_seconds": 1.0,
            "idle_poll_seconds": 1.0,
        }

        cfg_path = tmp_path / "config.json"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg_dict, f, indent=2)

        return cfg, cfg_path
    return _make


def test_windows_background_cli_lifecycle(make_config_file) -> None:
    config, config_path = make_config_file()

    status_cmd = [
        sys.executable,
        "-m", "bdb_bridge",
        "bridge", "status",
        "--config", str(config_path),
        "--json"
    ]

    # 1. Start background process
    start_cmd = [
        sys.executable,
        "-m", "bdb_bridge",
        "bridge", "start",
        "--config", str(config_path),
        "--background"
    ]

    res = subprocess.run(start_cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert "Service started in background successfully" in res.stdout

    # 2. Check status shows RUNNING and PID is alive
    res_status = subprocess.run(status_cmd, capture_output=True, text=True)
    assert res_status.returncode == 0
    status_data = json.loads(res_status.stdout.strip())
    assert status_data["status"] == "RUNNING"
    assert status_data["lock_held"] is True

    bg_pid = status_data["pid"]
    assert bg_pid is not None
    assert bg_pid > 0
    assert is_pid_alive(bg_pid) is True

    # 3. Double start rejection
    res_double = subprocess.run(start_cmd, capture_output=True, text=True)
    assert res_double.returncode == 1
    assert "Error: Another bridge instance is already running" in res_double.stderr

    # 4. Request Graceful Stop
    stop_cmd = [
        sys.executable,
        "-m", "bdb_bridge",
        "bridge", "stop",
        "--config", str(config_path),
    ]
    res_stop = subprocess.run(stop_cmd, capture_output=True, text=True)
    assert res_stop.returncode == 0
    assert "Graceful stop request sent successfully" in res_stop.stdout

    # 5. Wait until OFFLINE and process is no longer alive
    start_time = time.time()
    stopped = False
    while time.time() - start_time < 10.0:
        res_status = subprocess.run(status_cmd, capture_output=True, text=True)
        status_data = json.loads(res_status.stdout.strip())
        if status_data["status"] == "OFFLINE":
            stopped = True
            break
        time.sleep(0.5)

    assert stopped is True
    assert is_pid_alive(bg_pid) is False
