from __future__ import annotations

import threading
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from bdb_bridge import (
    BridgeService,
    Journal,
    BridgeConfig,
    CommandIngestor,
    SingleQueueScheduler,
    ResultCoordinator,
    OutboxProcessor,
    InstanceLock,
)
from bdb_bridge.models import (
    CommandRecord,
    PollReport,
    OutboxProcessOutcome,
    OutboxProcessState,
    ResultCoordinationOutcome,
    CommandState,
)


@pytest.fixture
def make_config(tmp_path: Path):
    return BridgeConfig(
        control_repo_path=tmp_path / "control",
        fixture_repo_path=tmp_path / "fixture",
        worktree_root=tmp_path / "worktree",
        runtime_dir=tmp_path / "runtime",
        idle_poll_seconds=0.1,
    )


def test_cycle_order_and_stop(tmp_path: Path, make_config) -> None:
    # Set up mocks for all dependencies to record call order and return mock data
    call_order = []

    # 1. Mock Journal
    journal = MagicMock(spec=Journal)
    journal._now_fn = lambda: "2026-07-15T12:00:00Z"
    
    # Simulate no active/recoverable command
    def get_rec():
        call_order.append("recovery_query")
        return None
    journal.get_recoverable_command.side_effect = get_rec
    
    # Simulate no blocking ingestion issues
    journal.has_blocking_ingestion_issues.return_value = False

    # 2. Mock Ingestor
    ingestor = MagicMock(spec=CommandIngestor)
    def poll():
        call_order.append("ingest")
        return PollReport(False, True, False, None, None, None, None)
    ingestor.poll_once.side_effect = poll

    # 3. Mock Scheduler
    scheduler = MagicMock(spec=SingleQueueScheduler)
    def claim():
        call_order.append("execute_claim")
        return None
    scheduler.claim_next.side_effect = claim

    # 4. Mock ResultCoordinator
    result_coordinator = MagicMock(spec=ResultCoordinator)

    # 5. Mock OutboxProcessor
    outbox_processor = MagicMock(spec=OutboxProcessor)
    def process_outbox():
        call_order.append("outbox")
        return OutboxProcessOutcome(OutboxProcessState.NO_DUE, None, None, None)
    outbox_processor.process_one_due.side_effect = process_outbox

    # 6. Mock InstanceLock
    lock = MagicMock(spec=InstanceLock)

    # Instantiate BridgeService
    waiter = threading.Event()
    service = BridgeService(
        config=make_config,
        journal=journal,
        ingestor=ingestor,
        scheduler=scheduler,
        result_coordinator=result_coordinator,
        outbox_processor=outbox_processor,
        instance_lock=lock,
        waiter=waiter,
    )

    # Run cycle once
    report = service.run_cycle("inst-1")
    
    # Assert exact order: recovery_query -> outbox -> ingest -> execute_claim
    assert call_order == ["recovery_query", "outbox", "ingest", "execute_claim"]
    assert report.recovery_outcome == "none"
    assert report.outbox_outcome == "none"
    assert report.ingest_outcome == "none"
    assert report.execute_outcome == "none"

    # Reset call order and test stop request after recovery
    call_order.clear()
    
    # Mock journal.get_recoverable_command to trigger a stop request during the run
    def get_rec_stop():
        call_order.append("recovery_query")
        service.request_stop()
        return None
    journal.get_recoverable_command.side_effect = get_rec_stop

    report = service.run_cycle("inst-1")
    # Should stop after recovery and not enter outbox, ingest, or execute
    assert call_order == ["recovery_query"]
    assert report.outbox_outcome is None
    assert report.ingest_outcome is None
    assert report.execute_outcome is None
