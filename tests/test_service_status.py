from __future__ import annotations

import json
import os
import time
from pathlib import Path
import pytest

from bdb_bridge import (
    Journal,
    BridgeConfig,
    ServiceStatusReader,
    ServiceStatus,
    ServiceInstanceState,
    InstanceLock,
)


@pytest.fixture
def make_config(tmp_path: Path):
    def _make(runtime_dir: Path):
        return BridgeConfig(
            control_repo_path=tmp_path / "control",
            fixture_repo_path=tmp_path / "fixture",
            worktree_root=tmp_path / "worktree",
            runtime_dir=runtime_dir,
            heartbeat_stale_seconds=3.0,
        )
    return _make


def test_status_scenarios(tmp_path: Path, make_config) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    config = make_config(runtime_dir)
    db_path = runtime_dir / "journal.db"
    
    # 1. OFFLINE - no record
    journal = Journal.open(db_path, now_fn=lambda: "2026-07-15T12:00:00Z")
    reader = ServiceStatusReader(config)
    
    status = reader.get_status(journal)
    assert status.status == ServiceStatus.OFFLINE
    assert status.instance_id is None
    assert status.pid is None
    assert status.lock_held is False
    assert status.diagnostic is None

    # 2. RUNNING
    # Acquire lock and register instance in running state
    lock = InstanceLock(runtime_dir / "bridge.instance.lock")
    assert lock.acquire() is True
    
    journal.start_service_instance("inst-1", pid=os.getpid(), started_at="2026-07-15T12:00:00Z")
    
    status = reader.get_status(journal)
    assert status.status == ServiceStatus.RUNNING
    assert status.instance_id == "inst-1"
    assert status.pid == os.getpid()
    assert status.lock_held is True
    assert status.diagnostic is None

    # 3. STOPPING
    journal.request_service_stop("inst-1")
    status = reader.get_status(journal)
    assert status.status == ServiceStatus.STOPPING
    assert status.instance_id == "inst-1"
    assert status.stop_requested is True

    # Complete stop -> OFFLINE
    journal.mark_service_instance_stopped("inst-1", exit_code=0)
    lock.release()
    
    status = reader.get_status(journal)
    assert status.status == ServiceStatus.OFFLINE
    assert status.instance_id == "inst-1"
    assert status.lock_held is False

    # 4. STALE - lock free but active record in DB
    # Start inst-2 but do not acquire lock
    journal.start_service_instance("inst-2", pid=os.getpid(), started_at="2026-07-15T12:00:00Z")
    status = reader.get_status(journal)
    assert status.status == ServiceStatus.STALE
    assert "lock file is not locked" in status.diagnostic

    # 5. STALE - heartbeat age stale
    # Acquire lock
    assert lock.acquire() is True
    # Now active record in DB and lock held. But we change time to simulate stale heartbeat.
    # Let's open journal with a time function that returns a future time
    journal.close()
    journal = Journal.open(db_path, now_fn=lambda: "2026-07-15T12:00:10Z") # 10s later, stale is 3.0
    status = reader.get_status(journal)
    assert status.status == ServiceStatus.STALE
    assert "exceeded stale threshold" in status.diagnostic

    # 6. STALE - dead PID
    # Reset journal time
    journal.close()
    journal = Journal.open(db_path, now_fn=lambda: "2026-07-15T12:00:00Z")
    # Mark stale by using a dead PID (e.g. 99999999 on Windows or 999999 on Unix)
    # Let's update instance in DB with a definitely dead PID
    journal._connection.execute(
        "UPDATE service_instances SET pid = ? WHERE instance_id = 'inst-2'",
        (99999999,),
    )
    journal._connection.commit()
    
    status = reader.get_status(journal)
    assert status.status == ServiceStatus.STALE
    assert "is not alive" in status.diagnostic

    # Clean up lock
    lock.release()
    journal.close()
