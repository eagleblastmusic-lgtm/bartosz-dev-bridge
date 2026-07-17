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
    BridgeCycleReport,
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
    call_order = []

    journal = MagicMock(spec=Journal)
    journal._now_fn = lambda: "2026-07-15T12:00:00Z"

    def get_rec():
        call_order.append("recovery_query")
        return None

    journal.get_recoverable_command.side_effect = get_rec
    journal.has_blocking_ingestion_issues.return_value = False

    ingestor = MagicMock(spec=CommandIngestor)

    def poll():
        call_order.append("ingest")
        return PollReport(False, True, False, None, None, None, None)

    ingestor.poll_once.side_effect = poll

    scheduler = MagicMock(spec=SingleQueueScheduler)

    def claim():
        call_order.append("execute_claim")
        return None

    scheduler.claim_next.side_effect = claim

    result_coordinator = MagicMock(spec=ResultCoordinator)

    outbox_processor = MagicMock(spec=OutboxProcessor)

    def process_outbox():
        call_order.append("outbox")
        return OutboxProcessOutcome(OutboxProcessState.NO_DUE, None, None, None)

    outbox_processor.process_one_due.side_effect = process_outbox

    lock = MagicMock(spec=InstanceLock)

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

    report = service.run_cycle("inst-1")

    assert call_order == ["recovery_query", "outbox", "ingest", "execute_claim"]
    assert report.recovery_outcome == "none"
    assert report.outbox_outcome == "none"
    assert report.ingest_outcome == "none"
    assert report.execute_outcome == "none"
    assert service._cycle_made_progress(report) is False

    call_order.clear()

    def get_rec_stop():
        call_order.append("recovery_query")
        service.request_stop()
        return None

    journal.get_recoverable_command.side_effect = get_rec_stop

    report = service.run_cycle("inst-1")
    assert call_order == ["recovery_query"]
    assert report.outbox_outcome is None
    assert report.ingest_outcome is None
    assert report.execute_outcome is None


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (BridgeCycleReport("none", "none", "none", "none", 1.0), False),
        (BridgeCycleReport("recovered:result_staged", "none", "none", "skipped", 1.0), False),
        (BridgeCycleReport("recovered:result_staged", "none", "ingested:1", "skipped", 1.0), True),
        (BridgeCycleReport("recovered:result_published", "none", "none", "skipped", 1.0), True),
        (BridgeCycleReport("none", "processed:retry_scheduled", "none", "none", 1.0), False),
        (BridgeCycleReport("none", "processed:published", "none", "none", 1.0), True),
        (BridgeCycleReport("none", "none", "ingested:1", "none", 1.0), True),
        (BridgeCycleReport("none", "none", "error:transport_unavailable", "none", 1.0), False),
        (BridgeCycleReport("none", "none", "none", "executed:result_staged", 1.0), False),
        (BridgeCycleReport("none", "none", "none", "executed:result_published", 1.0), True),
    ],
)
def test_cycle_progress_classification(report: BridgeCycleReport, expected: bool) -> None:
    assert BridgeService._cycle_made_progress(report) is expected
