from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import BridgeError, CommandState, OutboxState, SessionState
from bdb_bridge.execution import SystemCrash
from tests.helpers.result_outbox_fixture import COMMAND_ID, NOW, build_staged, make_journal, stage


def test_atomic_stage_replay_and_events(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    staged, result, outbox = stage(journal)
    assert result.result_sha256 == staged.result_sha256
    assert outbox.state == OutboxState.PENDING
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_STAGED
    for _ in range(10):
        replay = journal.stage_result_and_enqueue(command_id=COMMAND_ID, result_json=staged.result_json, remote_path=staged.remote_path)
        assert replay == (result, outbox)
    events = [event.event_type for event in journal.list_events(command_id=COMMAND_ID)]
    assert events.count("result.staged") == 1
    assert events.count("outbox.enqueued") == 1
    journal.close()


@pytest.mark.parametrize("point", ["AFTER_RESULT_INSERT", "AFTER_OUTBOX_INSERT", "BEFORE_RESULT_STAGED_TRANSITION", "BEFORE_STAGE_EVENTS"])
def test_stage_faults_roll_back_all(tmp_path: Path, point: str) -> None:
    journal = make_journal(tmp_path)
    staged = build_staged(journal)
    def crash(actual: str) -> None:
        if actual == point:
            raise SystemCrash(point)
    with pytest.raises(SystemCrash):
        journal.stage_result_and_enqueue(command_id=COMMAND_ID, result_json=staged.result_json, remote_path=staged.remote_path, fault_hook=crash)
    assert journal.get_result(COMMAND_ID) is None
    assert journal.get_outbox(COMMAND_ID) is None
    assert journal.get_command(COMMAND_ID).state == CommandState.EFFECT_RECORDED
    assert not any(e.event_type in {"result.staged", "outbox.enqueued"} for e in journal.list_events(command_id=COMMAND_ID))
    journal.close()


def test_result_collision_and_retry_backoff_persist(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = make_journal(tmp_path)
    staged, _, _ = stage(journal)
    with pytest.raises(BridgeError):
        journal.stage_result_and_enqueue(command_id=COMMAND_ID, result_json=staged.result_json.replace("3 passed", "4 passed"), remote_path=staged.remote_path)
    for attempt, expected_suffix in enumerate(("01Z", "02Z", "04Z", "08Z")):
        record = journal.record_outbox_failure(COMMAND_ID, expected_attempt_count=attempt, error_message="secret\x00   failure", now=NOW)
        assert record.attempt_count == attempt + 1
        assert record.next_attempt_at.endswith(expected_suffix)
    journal.close()
    reopened = __import__("bdb_bridge").Journal.open(path, now_fn=lambda: NOW)
    assert reopened.get_outbox(COMMAND_ID).attempt_count == 4
    assert reopened.get_result(COMMAND_ID).result_json == staged.result_json
    reopened.close()


def test_due_order_claim_cas_and_publish_replay(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    staged, _, outbox = stage(journal)
    due = journal.list_due_outbox(NOW, 10)
    assert [r.command_id for r in due] == [COMMAND_ID]
    claimed = journal.claim_due_outbox(NOW)
    assert claimed and claimed.attempt_count == 0
    assert journal.claim_due_outbox(NOW) is None
    published = journal.mark_result_published(
        COMMAND_ID,
        remote_result_sha256=staged.result_sha256,
        published_commit_sha="d" * 40,
        published_at=NOW,
    )
    assert published.state == OutboxState.PUBLISHED
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_PUBLISHED
    replay = journal.mark_result_published(
        COMMAND_ID,
        remote_result_sha256=staged.result_sha256,
        published_commit_sha="d" * 40,
        published_at=NOW,
    )
    assert replay == published
    assert [e.event_type for e in journal.list_events(command_id=COMMAND_ID)].count("result.published") == 1
    journal.close()


def test_collision_is_atomic_and_idempotent(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    stage(journal)
    collision = journal.mark_result_collision(COMMAND_ID, observed_result_sha256="sha256:" + "9" * 64, diagnostic="different")
    assert collision.state == OutboxState.COLLISION
    assert journal.get_command(COMMAND_ID).state == CommandState.MANUAL_RECONCILIATION_REQUIRED
    assert journal.get_session(collision.session_id).state == SessionState.MANUAL_RECONCILIATION_REQUIRED
    replay = journal.mark_result_collision(COMMAND_ID, observed_result_sha256="sha256:" + "9" * 64, diagnostic="different")
    assert replay == collision
    assert [e.event_type for e in journal.list_events(command_id=COMMAND_ID)].count("result.collision") == 1
    journal.close()


@pytest.mark.parametrize("point", ["AFTER_OUTBOX_PUBLISHED_BEFORE_COMMAND_TRANSITION", "BEFORE_PUBLISHED_EVENT"])
def test_publish_fault_rolls_back_all(tmp_path: Path, point: str) -> None:
    journal = make_journal(tmp_path)
    staged, _, _ = stage(journal)
    def crash(actual: str) -> None:
        if actual == point:
            raise SystemCrash(point)
    with pytest.raises(SystemCrash):
        journal.mark_result_published(
            COMMAND_ID,
            remote_result_sha256=staged.result_sha256,
            published_commit_sha="d" * 40,
            published_at=NOW,
            fault_hook=crash,
        )
    assert journal.get_outbox(COMMAND_ID).state == OutboxState.PENDING
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_STAGED
    assert not any(e.event_type == "result.published" for e in journal.list_events(command_id=COMMAND_ID))
    journal.close()


def test_collision_fault_rolls_back_all(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    stage(journal)
    def crash(actual: str) -> None:
        if actual == "AFTER_COLLISION_OUTBOX_BEFORE_MANUAL_STATE":
            raise SystemCrash(actual)
    with pytest.raises(SystemCrash):
        journal.mark_result_collision(
            COMMAND_ID,
            observed_result_sha256="sha256:" + "9" * 64,
            diagnostic="different",
            fault_hook=crash,
        )
    assert journal.get_outbox(COMMAND_ID).state == OutboxState.PENDING
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_STAGED
    assert journal.get_session(journal.get_outbox(COMMAND_ID).session_id).state == SessionState.ACTIVE
    journal.close()


def test_no_outbox_delete_api_and_error_is_bounded(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    stage(journal)
    assert not hasattr(journal, "delete_outbox")
    record = journal.record_outbox_failure(
        COMMAND_ID,
        expected_attempt_count=0,
        error_message="secret\x00 " + "x" * 5_000,
        now=NOW,
    )
    assert record.last_error is not None and len(record.last_error) <= 500
    assert "\x00" not in record.last_error
    journal.close()
