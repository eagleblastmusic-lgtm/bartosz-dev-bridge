from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeService,
    CommandState,
    ExecutionCoordinator,
    InstanceLock,
    Journal,
    OutboxProcessor,
    OutboxState,
    PollReport,
    ProfileRunOutcome,
    ResultCoordinator,
    SingleQueueScheduler,
)
from bdb_bridge.execution import SystemCrash
from tests.test_outbox_processor import FakeTransport
from tests.test_workspace_recovery_faults import COMMAND, NOW, SESSION, counts, setup

INSTANCE_ID = "inst-55555555-5555-4555-8555-555555555555"


class RecordingIngestor:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def poll_once(self) -> PollReport:
        self.order.append("ingest")
        return PollReport(False, True, False, None, None, None, None)


class RecordingOutboxProcessor(OutboxProcessor):
    def __init__(self, *args, order: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.order = order

    def process_one_due(self):
        self.order.append("outbox")
        return super().process_one_due()


class RecordingCoordinator(ResultCoordinator):
    def __init__(self, *args, order: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.order = order
        self.calls = 0

    def process(self, command_id: str):
        self.calls += 1
        self.order.append("recovery")
        return super().process(command_id)


class NoClaimScheduler:
    def __init__(self) -> None:
        self.calls = 0

    def claim_next(self):
        self.calls += 1
        return None


def crash_at(point: str):
    def hook(actual: str) -> None:
        if actual == point:
            raise SystemCrash(point)

    return hook


def prepare_state(
    root: Path,
    state: CommandState,
    monkeypatch: pytest.MonkeyPatch,
    profile_calls: list[str],
):
    cfg, db, _base = setup(root)

    def profile(self, workspace_manager, profile_id: str = "poc_pytest") -> ProfileRunOutcome:
        profile_calls.append(profile_id)
        return ProfileRunOutcome("success", 0, "3 passed", "", 1)

    monkeypatch.setattr(ExecutionCoordinator, "_run_profile", profile)

    if state == CommandState.CLAIMED:
        return cfg, db

    journal = Journal.open(db, now_fn=lambda: NOW)
    try:
        if state == CommandState.EXECUTING:
            with pytest.raises(SystemCrash):
                ExecutionCoordinator(
                    cfg,
                    journal,
                    fault_hook=crash_at("AFTER_PLAN_COMMIT_BEFORE_WRITE"),
                ).execute_or_recover(COMMAND)
        elif state == CommandState.EFFECT_RECORDED:
            with pytest.raises(SystemCrash):
                ExecutionCoordinator(
                    cfg,
                    journal,
                    fault_hook=crash_at("AFTER_EFFECT_COMMIT_BEFORE_PROFILE"),
                ).execute_or_recover(COMMAND)
        elif state == CommandState.RESULT_STAGED:
            processor = OutboxProcessor(journal, FakeTransport(), now_fn=lambda: NOW)
            coordinator = ResultCoordinator(
                cfg,
                journal,
                processor,
                now_fn=lambda: NOW,
                fault_hook=crash_at("AFTER_STAGE_COMMIT_BEFORE_PUBLISH"),
            )
            with pytest.raises(SystemCrash):
                coordinator.process(COMMAND)
        else:
            raise AssertionError(state)

        persisted = journal.get_command(COMMAND)
        assert persisted is not None and persisted.state == state
    finally:
        journal.close()
    return cfg, db


@pytest.mark.parametrize(
    "durable_state",
    [
        CommandState.CLAIMED,
        CommandState.EXECUTING,
        CommandState.EFFECT_RECORDED,
        CommandState.RESULT_STAGED,
    ],
)
def test_service_reopens_and_recovers_all_durable_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_state: CommandState,
) -> None:
    profile_calls: list[str] = []
    root = tmp_path / durable_state.value
    cfg, db = prepare_state(root, durable_state, monkeypatch, profile_calls)
    profiles_before_reopen = len(profile_calls)

    # All preparation objects are closed above. Recovery starts from a new
    # Journal and new service/coordinator objects only.
    journal = Journal.open(db, now_fn=lambda: NOW)
    order: list[str] = []
    transport = FakeTransport()
    processor = RecordingOutboxProcessor(
        journal,
        transport,
        now_fn=lambda: NOW,
        order=order,
    )

    if durable_state == CommandState.RESULT_STAGED:
        def forbidden_execution(_config, _journal, _hook):
            raise AssertionError("RESULT_STAGED recovery must not execute or run the profile")

        execution_factory = forbidden_execution
    else:
        execution_factory = None

    coordinator = RecordingCoordinator(
        cfg,
        journal,
        processor,
        now_fn=lambda: NOW,
        execution_factory=execution_factory,
        order=order,
    )
    scheduler = NoClaimScheduler()
    service = BridgeService(
        config=cfg,
        journal=journal,
        ingestor=RecordingIngestor(order),
        scheduler=scheduler,
        result_coordinator=coordinator,
        outbox_processor=processor,
        instance_lock=InstanceLock(Path(cfg.runtime_dir) / "bridge.instance.lock"),
    )

    report = service.run_cycle(INSTANCE_ID)

    command = journal.get_command(COMMAND)
    workspace = journal.get_workspace(SESSION)
    outbox = journal.get_outbox(COMMAND)
    assert command is not None and command.state == CommandState.RESULT_PUBLISHED
    assert workspace is not None and workspace.revision == 1
    assert outbox is not None and outbox.state == OutboxState.PUBLISHED
    assert counts(journal)[:2] == (1, 1)
    target = Path(cfg.worktree_root) / SESSION / "src" / "clamp.py"
    assert target.read_text(encoding="utf-8").count("return max(0, min(100, value))") == 1
    assert transport.pushes == 1

    if durable_state == CommandState.RESULT_STAGED:
        assert coordinator.calls == 0
        assert order[:2] == ["outbox", "ingest"]
        assert len(profile_calls) == profiles_before_reopen == 1
        assert report.recovery_outcome == "none"
    else:
        assert coordinator.calls == 1
        assert order[:3] == ["recovery", "outbox", "ingest"]
        assert len(profile_calls) == 1
        assert report.recovery_outcome == "recovered:result_staged"

    assert scheduler.calls == (1 if durable_state == CommandState.RESULT_STAGED else 0)
    journal.close()


def test_after_execute_claim_reopens_without_second_claim_or_duplicate_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_calls: list[str] = []
    cfg, db, _base = setup(tmp_path / "after-execute-claim")

    def profile(self, workspace_manager, profile_id: str = "poc_pytest") -> ProfileRunOutcome:
        profile_calls.append(profile_id)
        return ProfileRunOutcome("success", 0, "3 passed", "", 1)

    monkeypatch.setattr(ExecutionCoordinator, "_run_profile", profile)

    journal = Journal.open(db, now_fn=lambda: NOW)
    journal._connection.execute(
        "UPDATE commands SET state = ? WHERE command_id = ?",
        (CommandState.VALIDATED.value, COMMAND),
    )
    transport = FakeTransport()
    processor = OutboxProcessor(journal, transport, now_fn=lambda: NOW)
    coordinator = ResultCoordinator(cfg, journal, processor, now_fn=lambda: NOW)
    service = BridgeService(
        config=cfg,
        journal=journal,
        ingestor=RecordingIngestor([]),
        scheduler=SingleQueueScheduler(journal),
        result_coordinator=coordinator,
        outbox_processor=processor,
        instance_lock=InstanceLock(Path(cfg.runtime_dir) / "bridge.instance.lock"),
        fault_hook=crash_at("AFTER_EXECUTE_CLAIM"),
    )

    with pytest.raises(SystemCrash):
        service.run_cycle(INSTANCE_ID)

    claimed = journal.get_command(COMMAND)
    assert claimed is not None and claimed.state == CommandState.CLAIMED
    assert counts(journal)[:2] == (0, 0)
    assert profile_calls == []
    journal.close()

    # A completely new Journal/service graph resumes the durable CLAIMED row.
    reopened = Journal.open(db, now_fn=lambda: NOW)
    order: list[str] = []
    resumed_processor = RecordingOutboxProcessor(
        reopened,
        transport,
        now_fn=lambda: NOW,
        order=order,
    )
    resumed_coordinator = RecordingCoordinator(
        cfg,
        reopened,
        resumed_processor,
        now_fn=lambda: NOW,
        order=order,
    )
    resumed = BridgeService(
        config=cfg,
        journal=reopened,
        ingestor=RecordingIngestor(order),
        scheduler=NoClaimScheduler(),
        result_coordinator=resumed_coordinator,
        outbox_processor=resumed_processor,
        instance_lock=InstanceLock(Path(cfg.runtime_dir) / "bridge.instance.lock"),
    )

    report = resumed.run_cycle(INSTANCE_ID)
    command = reopened.get_command(COMMAND)
    workspace = reopened.get_workspace(SESSION)
    assert command is not None and command.state == CommandState.RESULT_PUBLISHED
    assert workspace is not None and workspace.revision == 1
    assert counts(reopened)[:2] == (1, 1)
    assert profile_calls == ["poc_pytest"]
    assert transport.pushes == 1
    assert report.recovery_outcome == "recovered:result_staged"

    claim_events = reopened._connection.execute(
        """
        SELECT COUNT(*) FROM events
        WHERE command_id = ?
          AND event_type = 'command.state_changed'
          AND payload_json LIKE '%\"to_state\":\"claimed\"%'
        """,
        (COMMAND,),
    ).fetchone()[0]
    assert claim_events == 1

    target = Path(cfg.worktree_root) / SESSION / "src" / "clamp.py"
    assert target.read_text(encoding="utf-8").count("return max(0, min(100, value))") == 1
    reopened.close()
