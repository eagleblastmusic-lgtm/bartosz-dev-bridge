from __future__ import annotations

import threading
import time
from pathlib import Path

from bdb_bridge import (
    BridgeConfig,
    BridgeService,
    CommandRecord,
    CommandState,
    InstanceLock,
    Journal,
    OutboxProcessOutcome,
    OutboxProcessState,
    PollReport,
    ResultCoordinationOutcome,
    ServiceInstanceState,
)

INSTANCE_ID = "inst-44444444-4444-4444-8444-444444444444"


class NoopIngestor:
    def poll_once(self) -> PollReport:
        return PollReport(False, True, False, None, None, None, None)


class OneCommandScheduler:
    def __init__(self) -> None:
        self._returned = False
        self.command = CommandRecord(
            command_id="cmd-blocking",
            session_id="session-blocking",
            sequence=1,
            command_sha256="sha256:" + "a" * 64,
            command_json="{}",
            command_commit_sha="b" * 40,
            state=CommandState.CLAIMED,
            expected_revision=0,
            expected_state_hash=None,
            created_at="2026-07-15T12:00:00Z",
            updated_at="2026-07-15T12:00:00Z",
        )

    def claim_next(self) -> CommandRecord | None:
        if self._returned:
            return None
        self._returned = True
        return self.command


class BlockingCoordinator:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def process(self, command_id: str) -> ResultCoordinationOutcome:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=8.0)
        return ResultCoordinationOutcome(
            command_id=command_id,
            command_state=CommandState.RESULT_STAGED,
            staged=True,
        )


class NoopOutbox:
    def process_one_due(self) -> OutboxProcessOutcome:
        return OutboxProcessOutcome(OutboxProcessState.NO_DUE, None, None, None)


def read_heartbeat(db_path: Path) -> tuple[str, ServiceInstanceState]:
    reader = Journal.open(db_path)
    try:
        record = reader.get_service_instance(INSTANCE_ID)
        assert record is not None
        return record.heartbeat_at, record.state
    finally:
        reader.close()


def test_stop_during_long_execute_keeps_heartbeat_and_skips_idle_delay(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    worktrees = tmp_path / "worktrees"
    runtime.mkdir()
    worktrees.mkdir()
    durable_marker = worktrees / "durable.marker"
    durable_marker.write_bytes(b"durable")
    db_path = runtime / "journal.db"

    config = BridgeConfig(
        control_repo_path=tmp_path / "control",
        fixture_repo_path=tmp_path / "fixture",
        worktree_root=worktrees,
        runtime_dir=runtime,
        journal_path=db_path,
        heartbeat_interval_seconds=0.1,
        heartbeat_stale_seconds=3.0,
        idle_poll_seconds=3.0,
    )
    journal = Journal.open(db_path)
    lock = InstanceLock(runtime / "bridge.instance.lock")
    assert lock.acquire() is True
    coordinator = BlockingCoordinator()
    service = BridgeService(
        config=config,
        journal=journal,
        ingestor=NoopIngestor(),
        scheduler=OneCommandScheduler(),
        result_coordinator=coordinator,
        outbox_processor=NoopOutbox(),
        instance_lock=lock,
    )

    outcomes = []
    errors: list[BaseException] = []

    def run_service() -> None:
        try:
            outcomes.append(service.run(INSTANCE_ID))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_service, name="service-test-thread")
    thread.start()
    assert coordinator.entered.wait(timeout=5.0)

    first, state = read_heartbeat(db_path)
    assert state == ServiceInstanceState.RUNNING
    observed = [first]
    deadline = time.monotonic() + 4.0
    while len(set(observed)) < 3 and time.monotonic() < deadline:
        time.sleep(0.12)
        heartbeat, current_state = read_heartbeat(db_path)
        assert current_state == ServiceInstanceState.RUNNING
        observed.append(heartbeat)
    assert len(set(observed)) >= 3

    stopper = Journal.open(db_path)
    try:
        stop = stopper.request_service_stop(INSTANCE_ID)
        assert stop.stop_requested is True
        stopping = stopper.get_service_instance(INSTANCE_ID)
        assert stopping is not None
        assert stopping.state == ServiceInstanceState.STOPPING
    finally:
        stopper.close()

    released_at = time.monotonic()
    coordinator.release.set()
    thread.join(timeout=5.0)
    elapsed_after_execute = time.monotonic() - released_at

    assert not thread.is_alive()
    assert errors == []
    assert coordinator.calls == 1
    assert len(outcomes) == 1 and outcomes[0].exit_code == 0
    assert elapsed_after_execute < 1.5

    final = journal.get_service_instance(INSTANCE_ID)
    assert final is not None
    assert final.state == ServiceInstanceState.STOPPED
    assert final.exit_code == 0
    assert db_path.exists()
    assert worktrees.is_dir()
    assert durable_marker.read_bytes() == b"durable"
    assert not any(t.name == f"heartbeat-{INSTANCE_ID}" for t in threading.enumerate())

    journal.close()
    lock.release()
    verifier = InstanceLock(runtime / "bridge.instance.lock")
    assert verifier.acquire() is True
    verifier.release()
