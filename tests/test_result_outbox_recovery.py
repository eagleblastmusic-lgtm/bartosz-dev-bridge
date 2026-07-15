from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import (
    CommandState,
    OutboxProcessState,
    OutboxProcessor,
    ResultCoordinator,
)
from bdb_bridge.execution import SystemCrash
from tests.helpers.result_outbox_fixture import COMMAND_ID, NOW, make_journal, make_outcome, stage
from tests.test_outbox_processor import FakeTransport


class FakeExecution:
    def __init__(self, outcome, calls: list[str]) -> None:
        self.outcome = outcome
        self.calls = calls

    def execute_or_recover(self, command_id: str):
        self.calls.append(command_id)
        return self.outcome


def test_effect_to_stage_then_staged_never_executes_again(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    transport = FakeTransport()
    processor = OutboxProcessor(journal, transport, now_fn=lambda: NOW)
    calls: list[str] = []
    coordinator = ResultCoordinator(
        object(),
        journal,
        processor,
        now_fn=lambda: NOW,
        execution_factory=lambda _config, _journal, _hook: FakeExecution(make_outcome(), calls),
    )
    first = coordinator.process(COMMAND_ID)
    assert first.command_state == CommandState.RESULT_STAGED
    assert calls == [COMMAND_ID]
    second = coordinator.process(COMMAND_ID)
    assert second.command_state == CommandState.RESULT_PUBLISHED
    assert calls == [COMMAND_ID]
    journal.close()


def test_crash_after_result_built_rolls_back_to_effect_recorded(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    transport = FakeTransport()
    calls: list[str] = []
    def crash(point: str) -> None:
        if point == "AFTER_RESULT_BUILT_BEFORE_STAGE":
            raise SystemCrash(point)
    coordinator = ResultCoordinator(
        object(), journal, OutboxProcessor(journal, transport, now_fn=lambda: NOW),
        now_fn=lambda: NOW, fault_hook=crash,
        execution_factory=lambda _config, _journal, _hook: FakeExecution(make_outcome(), calls),
    )
    with pytest.raises(SystemCrash):
        coordinator.process(COMMAND_ID)
    assert journal.get_result(COMMAND_ID) is None
    assert journal.get_outbox(COMMAND_ID) is None
    assert journal.get_command(COMMAND_ID).state == CommandState.EFFECT_RECORDED
    journal.close()


def test_push_success_crash_before_ack_reconciles_without_second_push(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = make_journal(tmp_path)
    staged, _, _ = stage(journal)
    transport = FakeTransport()
    def crash(point: str) -> None:
        if point == "AFTER_REMOTE_PUSH_BEFORE_LOCAL_ACK":
            raise SystemCrash(point)
    with pytest.raises(SystemCrash):
        OutboxProcessor(journal, transport, now_fn=lambda: NOW, fault_hook=crash).process_command(COMMAND_ID)
    assert transport.remote == staged.result_bytes
    assert transport.pushes == 1
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_STAGED
    journal.close()

    from bdb_bridge import Journal
    reopened = Journal.open(path, now_fn=lambda: NOW)
    outcome = OutboxProcessor(reopened, transport, now_fn=lambda: NOW).process_command(COMMAND_ID)
    assert outcome.state == OutboxProcessState.PUBLISHED
    assert transport.pushes == 1
    assert reopened.get_command(COMMAND_ID).state == CommandState.RESULT_PUBLISHED
    reopened.close()


def test_crash_after_stage_restarts_as_publish_only(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = make_journal(tmp_path)
    transport = FakeTransport()
    calls: list[str] = []
    def crash(point: str) -> None:
        if point == "AFTER_STAGE_COMMIT_BEFORE_PUBLISH":
            raise SystemCrash(point)
    coordinator = ResultCoordinator(
        object(), journal, OutboxProcessor(journal, transport, now_fn=lambda: NOW),
        now_fn=lambda: NOW, fault_hook=crash,
        execution_factory=lambda _config, _journal, _hook: FakeExecution(make_outcome(), calls),
    )
    with pytest.raises(SystemCrash):
        coordinator.process(COMMAND_ID)
    assert calls == [COMMAND_ID]
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_STAGED
    staged_bytes = journal.get_result(COMMAND_ID).result_json.encode("utf-8")
    journal.close()

    from bdb_bridge import Journal
    reopened = Journal.open(path, now_fn=lambda: NOW)
    second_calls: list[str] = []
    resumed = ResultCoordinator(
        object(), reopened, OutboxProcessor(reopened, transport, now_fn=lambda: NOW),
        now_fn=lambda: NOW,
        execution_factory=lambda _config, _journal, _hook: FakeExecution(make_outcome(), second_calls),
    ).process(COMMAND_ID)
    assert resumed.command_state == CommandState.RESULT_PUBLISHED
    assert second_calls == []
    assert transport.remote == staged_bytes
    reopened.close()
