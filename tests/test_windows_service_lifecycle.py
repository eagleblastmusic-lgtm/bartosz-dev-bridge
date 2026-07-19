from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from bdb_bridge import Journal, ServiceInstanceState, is_pid_alive
from tests.helpers.service_lifecycle_fixture import (
    assert_lock_is_free,
    cli_command,
    make_service_config,
    read_status,
    wait_for_heartbeat_change,
    wait_for_status,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows background service mode is only supported on Windows",
)


def test_windows_background_cli_lifecycle_full_contract(tmp_path: Path) -> None:
    config, config_path, durable_marker = make_service_config(
        tmp_path,
        heartbeat_interval_seconds=0.2,
        heartbeat_stale_seconds=5.0,
        idle_poll_seconds=2.0,
    )
    start_cmd = cli_command(config_path, "start", "--background")
    stop_cmd = cli_command(config_path, "stop")
    journal_path = Path(config.journal_path)
    lock_path = Path(config.runtime_dir) / "bridge.instance.lock"

    initial = read_status(config_path)
    assert initial["status"] == "OFFLINE"
    assert initial["lock_held"] is False

    first = subprocess.run(start_cmd, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert "Service started in background successfully" in first.stdout

    running = wait_for_status(config_path, "RUNNING")
    bg_pid = running["pid"]
    assert isinstance(bg_pid, int) and bg_pid > 0
    assert running["instance_id"].startswith("inst-")
    assert running["lock_held"] is True
    assert running["pid_alive"] is True
    assert is_pid_alive(bg_pid) is True

    first_heartbeat = running["heartbeat_at"]
    progressed = wait_for_heartbeat_change(config_path, first_heartbeat)
    assert progressed["heartbeat_at"] != first_heartbeat
    assert progressed["pid"] == bg_pid

    second = subprocess.run(start_cmd, capture_output=True, text=True, check=False)
    assert second.returncode == 1
    assert "Another bridge instance is already running" in second.stderr

    stop = subprocess.run(stop_cmd, capture_output=True, text=True, check=False)
    assert stop.returncode == 0
    assert "Graceful stop request sent successfully" in stop.stdout

    deadline = time.monotonic() + 12.0
    final_status = None
    while time.monotonic() < deadline:
        final_status = read_status(config_path)
        if final_status["status"] == "OFFLINE":
            break
        time.sleep(0.05)

    assert final_status is not None and final_status["status"] == "OFFLINE"
    assert is_pid_alive(bg_pid) is False

    assert journal_path.exists()
    assert Path(config.worktree_root).is_dir()
    assert durable_marker.read_bytes() == b"keep-me"
    assert lock_path.exists()
    assert_lock_is_free(config)

    journal = Journal.open(journal_path)
    latest = journal.get_latest_service_instance()
    assert latest is not None
    assert latest.pid == bg_pid
    assert latest.state == ServiceInstanceState.STOPPED
    assert latest.stop_requested_at is not None
    assert latest.stopped_at is not None
    assert latest.exit_code == 0
    journal.close()

    from bdb_bridge import cli, service

    source = Path(cli.__file__).read_text(encoding="utf-8") + Path(service.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "taskkill",
        "TerminateProcess",
        "powershell",
        "schtasks",
        "CreateService",
        "Start-Service",
    ):
        assert forbidden.lower() not in source.lower()
