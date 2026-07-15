from __future__ import annotations

import json
from pathlib import Path
import pytest
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
    CommandState,
    ResultCoordinationOutcome,
    OutboxProcessOutcome,
    OutboxProcessState,
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


def test_recovery_claimed_command(tmp_path: Path, make_config) -> None:
    journal = MagicMock(spec=Journal)
    journal._now_fn = lambda: "2026-07-15T12:00:00Z"

    # Simulate a command in CLAIMED state returned by get_recoverable_command
    claimed_cmd = CommandRecord(
        command_id="cmd-1",
        session_id="sess-1",
        sequence=1,
        command_sha256="sha256_1",
        command_json="{}",
        command_commit_sha="c_sha",
        state=CommandState.CLAIMED,
        expected_revision=0,
        expected_state_hash="h1",
        created_at="2026-07-15T12:00:00Z",
        updated_at="2026-07-15T12:00:00Z",
    )
    journal.get_recoverable_command.return_value = claimed_cmd
    journal.has_blocking_ingestion_issues.return_value = False

    # Mock ResultCoordinator to handle processing
    result_coordinator = MagicMock(spec=ResultCoordinator)
    result_coordinator.process.return_value = ResultCoordinationOutcome(
        command_id="cmd-1",
        command_state=CommandState.RESULT_STAGED,
        staged=True,
    )

    # Ingestor mock
    ingestor = MagicMock(spec=CommandIngestor)
    scheduler = MagicMock(spec=SingleQueueScheduler)
    outbox_processor = MagicMock(spec=OutboxProcessor)
    outbox_processor.process_one_due.return_value = OutboxProcessOutcome(OutboxProcessState.NO_DUE, None, None, None)
    lock = MagicMock(spec=InstanceLock)

    service = BridgeService(
        config=make_config,
        journal=journal,
        ingestor=ingestor,
        scheduler=scheduler,
        result_coordinator=result_coordinator,
        outbox_processor=outbox_processor,
        instance_lock=lock,
    )

    # Run cycle
    report = service.run_cycle("inst-1")

    # Assert recovery called process on the claimed command
    result_coordinator.process.assert_called_once_with("cmd-1")
    assert report.recovery_outcome == "recovered:result_staged"
    # execute phase is skipped since there was a recoverable active command
    assert report.execute_outcome == "skipped"
