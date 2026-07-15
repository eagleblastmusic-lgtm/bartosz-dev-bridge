from __future__ import annotations

import sqlite3
import time
from pathlib import Path
import pytest

from bdb_bridge import Journal, BridgeError, BridgeErrorCode, HeartbeatWorker
from bdb_bridge.models import (
    ServiceInstanceState,
    ServiceStatus,
)

FIXED_NOW = "2026-07-15T12:00:00Z"


def fixed_now() -> str:
    return FIXED_NOW


def test_service_journal_flow(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    # 1. Initially no active/latest service instance
    assert journal.get_active_service_instance() is None
    assert journal.get_latest_service_instance() is None

    # 2. Start a service instance
    inst1 = journal.start_service_instance(
        instance_id="inst-11111111-1111-1111-1111-111111111111",
        pid=123,
        started_at="2026-07-15T12:00:00Z",
    )
    assert inst1.instance_id == "inst-11111111-1111-1111-1111-111111111111"
    assert inst1.pid == 123
    assert inst1.state == ServiceInstanceState.RUNNING
    assert inst1.started_at == "2026-07-15T12:00:00Z"

    # Get active and latest
    active = journal.get_active_service_instance()
    assert active is not None
    assert active.instance_id == "inst-11111111-1111-1111-1111-111111111111"

    latest = journal.get_latest_service_instance()
    assert latest is not None
    assert latest.instance_id == "inst-11111111-1111-1111-1111-111111111111"

    # 3. Starting another active instance fails
    with pytest.raises(BridgeError) as exc:
        journal.start_service_instance(
            instance_id="inst-22222222-2222-2222-2222-222222222222",
            pid=456,
            started_at="2026-07-15T12:00:05Z",
        )
    assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT

    # 4. Heartbeat updates timestamps
    journal.heartbeat_service_instance("inst-11111111-1111-1111-1111-111111111111")
    inst1_hb = journal.get_service_instance("inst-11111111-1111-1111-1111-111111111111")
    assert inst1_hb is not None
    assert inst1_hb.heartbeat_at == FIXED_NOW

    # 5. Heartbeat for non-existent fails
    with pytest.raises(BridgeError) as exc:
        journal.heartbeat_service_instance("inst-00000000-0000-0000-0000-000000000000")
    assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT

    # 6. Request stop transitions running -> stopping
    stop_outcome = journal.request_service_stop("inst-11111111-1111-1111-1111-111111111111")
    assert stop_outcome.instance_id == "inst-11111111-1111-1111-1111-111111111111"
    assert stop_outcome.status == ServiceStatus.STOPPING
    assert stop_outcome.stop_requested is True

    inst1_stopping = journal.get_service_instance("inst-11111111-1111-1111-1111-111111111111")
    assert inst1_stopping is not None
    assert inst1_stopping.state == ServiceInstanceState.STOPPING
    assert inst1_stopping.stop_requested_at == FIXED_NOW

    # Stop request is idempotent
    stop_outcome_again = journal.request_service_stop("inst-11111111-1111-1111-1111-111111111111")
    assert stop_outcome_again.status == ServiceStatus.STOPPING

    # 7. Stop service instance
    journal.mark_service_instance_stopped("inst-11111111-1111-1111-1111-111111111111", exit_code=0)
    inst1_stopped = journal.get_service_instance("inst-11111111-1111-1111-1111-111111111111")
    assert inst1_stopped is not None
    assert inst1_stopped.state == ServiceInstanceState.STOPPED
    assert inst1_stopped.stopped_at == FIXED_NOW
    assert inst1_stopped.exit_code == 0

    # Stop is idempotent
    journal.mark_service_instance_stopped("inst-11111111-1111-1111-1111-111111111111", exit_code=0)

    # Cannot heartbeat stopped instance
    with pytest.raises(BridgeError) as exc:
        journal.heartbeat_service_instance("inst-11111111-1111-1111-1111-111111111111")
    assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT

    # 8. Start a new instance after previous stopped
    inst2 = journal.start_service_instance(
        instance_id="inst-22222222-2222-2222-2222-222222222222",
        pid=456,
        started_at="2026-07-15T12:05:00Z",
    )
    assert inst2.instance_id == "inst-22222222-2222-2222-2222-222222222222"
    assert journal.get_active_service_instance().instance_id == "inst-22222222-2222-2222-2222-222222222222"

    # Mark failed
    journal.mark_service_instance_failed("inst-22222222-2222-2222-2222-222222222222", error="forced error")
    inst2_failed = journal.get_service_instance("inst-22222222-2222-2222-2222-222222222222")
    assert inst2_failed is not None
    assert inst2_failed.state == ServiceInstanceState.FAILED
    assert inst2_failed.last_error == "forced error"

    # 9. Mark abandoned stale
    # Start inst-3
    journal.start_service_instance("inst-33333333-3333-3333-3333-333333333333", pid=789, started_at="2026-07-15T12:10:00Z")
    assert journal.get_active_service_instance().instance_id == "inst-33333333-3333-3333-3333-333333333333"
    
    stale_count = journal.mark_abandoned_service_instances_stale("process crashed")
    assert stale_count == 1
    inst3_stale = journal.get_service_instance("inst-33333333-3333-3333-3333-333333333333")
    assert inst3_stale is not None
    assert inst3_stale.state == ServiceInstanceState.STALE
    assert inst3_stale.last_error == "process crashed"

    # 10. Check events were appended (started, stop_requested, stopped, started, failed, started, stale_detected)
    events = journal._conn.execute(
        "SELECT event_type FROM events WHERE event_type LIKE 'service.%' ORDER BY event_id"
    ).fetchall()
    assert [r[0] for r in events] == [
        "service.started",
        "service.stop_requested",
        "service.stopped",
        "service.started",
        "service.failed",
        "service.started",
        "service.stale_detected",
    ]

    journal.close()


def test_get_recoverable_command_corruption(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    class FakeConnection:
        def execute(self, sql, *args):
            return self
        def fetchall(self):
            return [
                ("c1", "s1", 1, "sha1", "{}", None, "claimed", 0, "hash1", FIXED_NOW, FIXED_NOW),
                ("c2", "s1", 2, "sha2", "{}", None, "executing", 0, "hash2", FIXED_NOW, FIXED_NOW),
            ]

    original_conn = journal._conn
    journal._conn = FakeConnection()

    try:
        with pytest.raises(BridgeError) as exc:
            journal.get_recoverable_command()
        assert exc.value.code == BridgeErrorCode.JOURNAL_CORRUPT
    finally:
        journal._conn = original_conn
        journal.close()


def test_heartbeat_worker_multiple_ticks(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    inst = journal.start_service_instance(
        instance_id="inst-11111111-1111-1111-1111-111111111111",
        pid=123,
        started_at="2026-07-15T12:00:00Z",
    )

    # Start worker with a short interval
    # To check that heartbeat_at updates dynamically, we can pass a dynamically changing now_fn
    counter = 0
    def changing_time() -> str:
        nonlocal counter
        t = f"2026-07-15T12:00:{counter:02d}Z"
        counter += 1
        return t

    worker = HeartbeatWorker(
        db_path,
        "inst-11111111-1111-1111-1111-111111111111",
        interval_seconds=0.2,
        now_fn=changing_time,
    )

    worker.start()
    time.sleep(0.8) # wait for multiple ticks
    worker.stop()

    # Reload instance from DB
    reloaded = journal.get_service_instance("inst-11111111-1111-1111-1111-111111111111")
    assert reloaded is not None
    # Since interval is 0.2s and we slept 0.8s, there should be multiple updates
    # The heartbeat_at timestamp should have advanced beyond the start time
    assert reloaded.heartbeat_at != "2026-07-15T12:00:00Z"
    journal.close()


def test_heartbeat_worker_does_not_share_journal(tmp_path: Path) -> None:
    # HeartbeatWorker constructor takes journal_path: Path, not a Journal instance,
    # proving it does not share the main connection
    db_path = tmp_path / "journal.db"
    worker = HeartbeatWorker(
        db_path,
        "inst-11111111-1111-1111-1111-111111111111",
        interval_seconds=1.0,
    )
    assert worker.journal_path == db_path.expanduser().resolve()
