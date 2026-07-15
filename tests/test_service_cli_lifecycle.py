from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest

from bdb_bridge import Journal, ServiceStatusReader, ServiceStatus, InstanceLock, BridgeConfig


@pytest.fixture
def make_config_file(tmp_path: Path):
    def _make():
        control = tmp_path / "control"
        control.mkdir(parents=True, exist_ok=True)
        # Initialize bare-like or simple git repo to make transports happy
        subprocess.run(["git", "init"], cwd=control, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Configure local settings for tests
        subprocess.run(["git", "config", "user.name", "Test"], cwd=control)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=control)

        # Create an initial commit to have a valid HEAD
        (control / "dummy.txt").write_text("initial")
        subprocess.run(["git", "add", "dummy.txt"], cwd=control)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=control)

        # Create commands and results branch
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


def test_foreground_cli_lifecycle(make_config_file) -> None:
    config, config_path = make_config_file()

    # 1. Query status initially - should be OFFLINE
    status_cmd = [
        sys.executable,
        "-m", "bdb_bridge",
        "bridge", "status",
        "--config", str(config_path),
        "--json"
    ]
    res = subprocess.run(status_cmd, capture_output=True, text=True)
    assert res.returncode == 0
    status_data = json.loads(res.stdout.strip())
    assert status_data["status"] == "OFFLINE"
    assert status_data["lock_held"] is False
    assert status_data["pid"] is None

    # 2. Start the service process in foreground (using Popen since start --foreground blocks)
    start_cmd = [
        sys.executable,
        "-m", "bdb_bridge",
        "bridge", "start",
        "--config", str(config_path),
        "--foreground"
    ]

    proc = subprocess.Popen(
        start_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for the service to start and write RUNNING state
    start_time = time.time()
    running = False
    proc_pid = None

    while time.time() - start_time < 10.0:
        time.sleep(0.5)
        res_status = subprocess.run(status_cmd, capture_output=True, text=True)
        if res_status.returncode == 0:
            status_data = json.loads(res_status.stdout.strip())
            if status_data["status"] == "RUNNING":
                running = True
                proc_pid = status_data["pid"]
                break

        # If the process exited early, fail the test and print logs
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            pytest.fail(f"Service process exited prematurely. stdout: {stdout}, stderr: {stderr}")

    assert running is True
    assert proc_pid is not None
    from bdb_bridge import is_pid_alive
    assert is_pid_alive(proc_pid) is True

    # 3. Double start rejection (should fail lock acquisition)
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

    # 5. Verify transition to STOPPING, then clean exit to OFFLINE
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

    # Verify the child process terminated with exit code 0
    proc.wait(timeout=5.0)
    assert proc.returncode == 0
