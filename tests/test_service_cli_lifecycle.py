from __future__ import annotations

import os
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


def test_foreground_cli_lifecycle_full_contract(tmp_path: Path) -> None:
    config, config_path, durable_marker = make_service_config(
        tmp_path,
        heartbeat_interval_seconds=0.2,
        heartbeat_stale_seconds=5.0,
        idle_poll_seconds=2.0,
    )
    journal_path = Path(config.journal_path)
    worktree_root = Path(config.worktree_root)
    lock_path = Path(config.runtime_dir) / "bridge.instance.lock"

    initial = read_status(config_path)
    assert initial["status"] == "OFFLINE"
    assert initial["instance_id"] is None
    assert initial["pid"] is None
    assert initial["lock_held"] is False

    start_cmd = cli_command(config_path, "start", "--foreground")
    stop_cmd = cli_command(config_path, "stop")
    proc = subprocess.Popen(
        start_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        running = wait_for_status(config_path, "RUNNING")
        assert running["instance_id"].startswith("inst-")
        assert running["pid"] == proc.pid
        assert running["lock_held"] is True
        assert running["pid_alive"] is True
        assert is_pid_alive(proc.pid) is True
        first_heartbeat = running["heartbeat_at"]
        assert isinstance(first_heartbeat, str)

        progressed = wait_for_heartbeat_change(config_path, first_heartbeat)
        assert progressed["heartbeat_at"] != first_heartbeat
        assert progressed["instance_id"] == running["instance_id"]

        second = subprocess.run(start_cmd, capture_output=True, text=True, check=False)
        assert second.returncode == 1
        assert "Another bridge instance is already running" in second.stderr

        stop = subprocess.run(stop_cmd, capture_output=True, text=True, check=False)
        assert stop.returncode == 0
        assert "Graceful stop request sent successfully" in stop.stdout

        observed_stopping = False
        deadline = time.monotonic() + 4.0
        final_status = None
        while time.monotonic() < deadline:
            final_status = read_status(config_path)
            if final_status["status"] == "STOPPING":
                observed_stopping = True
            if final_status["status"] == "OFFLINE":
                break
            time.sleep(0.05)
        assert observed_stopping is True
        assert final_status is not None and final_status["status"] == "OFFLINE"

        proc.wait(timeout=8.0)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            subprocess.run(stop_cmd, capture_output=True, text=True, check=False)
            proc.wait(timeout=10.0)

    assert journal_path.exists()
    assert worktree_root.is_dir()
    assert durable_marker.read_bytes() == b"keep-me"
    assert lock_path.exists()
    assert_lock_is_free(config)

    journal = Journal.open(journal_path)
    latest = journal.get_latest_service_instance()
    assert latest is not None
    assert latest.state == ServiceInstanceState.STOPPED
    assert latest.exit_code == 0
    journal.close()

    from bdb_bridge import cli, service

    source = Path(cli.__file__).read_text(encoding="utf-8") + Path(service.__file__).read_text(encoding="utf-8")
    for forbidden in ("taskkill", "TerminateProcess", "powershell", "schtasks", "CreateService"):
        assert forbidden.lower() not in source.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows contract only")
def test_background_is_controlled_unsupported_on_non_windows(tmp_path: Path) -> None:
    _config, config_path, _marker = make_service_config(tmp_path)
    completed = subprocess.run(
        cli_command(config_path, "start", "--background"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "only supported on Windows" in completed.stderr


def test_fault_after_instance_lock_before_db_start_is_controlled_and_recoverable(
    tmp_path: Path,
) -> None:
    config, config_path, durable_marker = make_service_config(
        tmp_path,
        idle_poll_seconds=0.5,
    )
    journal_path = Path(config.journal_path)
    journal = Journal.open(journal_path)
    journal.close()

    env = os.environ.copy()
    env["BDB_FAULT_POINT"] = "AFTER_INSTANCE_LOCK_BEFORE_DB_START"
    faulted = subprocess.run(
        cli_command(config_path, "start", "--foreground"),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert faulted.returncode == 2
    assert "Controlled lifecycle fault" in faulted.stderr
    assert "AFTER_INSTANCE_LOCK_BEFORE_DB_START" in faulted.stderr

    reopened = Journal.open(journal_path)
    assert reopened.get_active_service_instance() is None
    reopened.close()
    assert journal_path.exists()
    assert durable_marker.read_bytes() == b"keep-me"
    assert_lock_is_free(config)

    start_cmd = cli_command(config_path, "start", "--foreground")
    stop_cmd = cli_command(config_path, "stop")
    proc = subprocess.Popen(
        start_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        running = wait_for_status(config_path, "RUNNING")
        assert running["pid"] == proc.pid
        stopped = subprocess.run(stop_cmd, capture_output=True, text=True, check=False)
        assert stopped.returncode == 0
        wait_for_status(config_path, "OFFLINE")
        proc.wait(timeout=8.0)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            subprocess.run(stop_cmd, capture_output=True, text=True, check=False)
            proc.wait(timeout=10.0)

    assert journal_path.exists()
    assert durable_marker.read_bytes() == b"keep-me"
    assert_lock_is_free(config)
