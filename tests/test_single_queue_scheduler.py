from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from bdb_bridge import (
    CommandIngestor,
    CommandSnapshot,
    CommandState,
    Journal,
    SessionState,
    SingleQueueScheduler,
)
from tests.helpers.git_control_repo import (
    SESSION_ID,
    SESSION_ID_B,
    fixed_now,
)
from tests.test_durable_ingestion import FakeTransport, make_snapshot, open_journal, ingest


def complete_command(journal: Journal, command_id: str) -> None:
    journal.transition_command(command_id, CommandState.CLAIMED, CommandState.EXECUTING)
    journal.transition_command(command_id, CommandState.EXECUTING, CommandState.RESULT_STAGED)


def prepare_validated_session(
    journal: Journal,
    session_id: str = SESSION_ID,
    sequences: tuple[int, ...] = (1,),
) -> None:
    ingest(journal, make_snapshot(session_id=session_id, sequences=sequences))


def test_does_not_claim_sequence_two_before_one(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    prepare_validated_session(journal, sequences=(1, 2))
    scheduler = SingleQueueScheduler(journal)
    claimed = scheduler.claim_next()
    assert claimed is not None
    assert claimed.sequence == 1
    journal.close()


def test_sequence_gap_blocks_queue(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    prepare_validated_session(journal, sequences=(1,))
    ingest(journal, make_snapshot(sequences=(1, 3), snapshot_sha="g" * 40))
    scheduler = SingleQueueScheduler(journal)
    first = scheduler.claim_next()
    complete_command(journal, first.command_id)
    assert scheduler.claim_next() is None
    journal.close()


def test_sequence_two_claimed_after_one_done(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    prepare_validated_session(journal, sequences=(1, 2))
    scheduler = SingleQueueScheduler(journal)
    first = scheduler.claim_next()
    assert first.sequence == 1
    complete_command(journal, first.command_id)
    second = scheduler.claim_next()
    assert second is not None
    assert second.sequence == 2
    journal.close()


def test_one_active_session_blocks_other(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    combined = CommandSnapshot(
        snapshot_sha="h" * 40,
        manifests=make_snapshot(session_id=SESSION_ID).manifests
        + make_snapshot(session_id=SESSION_ID_B).manifests,
        commands=make_snapshot(session_id=SESSION_ID, sequences=(1,)).commands
        + make_snapshot(session_id=SESSION_ID_B, sequences=(1,)).commands,
    )
    ingest(journal, combined)
    scheduler = SingleQueueScheduler(journal)
    first = scheduler.claim_next()
    assert first.session_id in {SESSION_ID, SESSION_ID_B}
    other = SESSION_ID_B if first.session_id == SESSION_ID else SESSION_ID
    complete_command(journal, first.command_id)
    journal.transition_session(first.session_id, SessionState.ACTIVE, SessionState.COMPLETING)
    journal.transition_session(first.session_id, SessionState.COMPLETING, SessionState.COMPLETED)
    next_claim = scheduler.claim_next()
    assert next_claim is not None
    assert next_claim.session_id == other
    journal.close()


def test_blocking_ingestion_issue_stops_claim(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    prepare_validated_session(journal)
    journal.record_ingestion_issue(
        source_id="commands",
        source_path=f"sessions/{SESSION_ID}/manifest.json",
        snapshot_sha="i" * 40,
        raw_sha256="sha256:" + "a" * 64,
        error_code="session_id_collision",
        detail="blocked",
        blocking=True,
        session_id=SESSION_ID,
    )
    scheduler = SingleQueueScheduler(journal)
    assert scheduler.claim_next() is None
    journal.close()


def test_expired_not_claimed(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    prepare_validated_session(journal)
    journal._connection.execute(
        "UPDATE commands SET state = ? WHERE command_id = ?",
        (CommandState.EXPIRED.value, f"{SESSION_ID}:000001"),
    )
    journal._connection.commit()
    assert SingleQueueScheduler(journal).claim_next() is None
    journal.close()


def test_rejected_not_claimed(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    prepare_validated_session(journal)
    journal._connection.execute(
        "UPDATE commands SET state = ? WHERE command_id = ?",
        (CommandState.REJECTED.value, f"{SESSION_ID}:000001"),
    )
    journal._connection.commit()
    assert SingleQueueScheduler(journal).claim_next() is None
    journal.close()


def test_restart_preserves_claimed(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)
    prepare_validated_session(journal)
    claimed = SingleQueueScheduler(journal).claim_next()
    journal.close()

    journal = Journal.open(path, now_fn=fixed_now)
    loaded = journal.get_command(claimed.command_id)
    assert loaded.state == CommandState.CLAIMED
    assert journal.get_session(SESSION_ID).state == SessionState.ACTIVE
    journal.close()


def test_parallel_schedulers_claim_at_most_one(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)
    prepare_validated_session(journal, sequences=(1, 2))
    journal.close()

    barrier = threading.Barrier(2)
    claims: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            local = Journal.open(path, now_fn=fixed_now)
            try:
                claim = SingleQueueScheduler(local).claim_next()
                if claim is not None:
                    claims.append(claim.command_id)
            finally:
                local.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert len(claims) == 1
    journal = Journal.open(path, now_fn=fixed_now)
    active_workers = journal._connection.execute(
        "SELECT COUNT(*) FROM commands WHERE state IN ('claimed','executing','effect_recorded')"
    ).fetchone()[0]
    assert active_workers == 1
    journal.close()


def test_fault_injection_before_commit_leaves_old_state(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    prepare_validated_session(journal)
    original = journal.get_command(f"{SESSION_ID}:000001")
    with patch.object(journal, "_append_event_in_transaction", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            journal.claim_next_command()
    assert journal._connection.in_transaction is False
    reloaded = journal.get_command(f"{SESSION_ID}:000001")
    assert reloaded.state == original.state
    journal.close()


def test_deterministic_session_ordering(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    combined = CommandSnapshot(
        snapshot_sha="j" * 40,
        manifests=make_snapshot(session_id=SESSION_ID_B).manifests
        + make_snapshot(session_id=SESSION_ID).manifests,
        commands=make_snapshot(session_id=SESSION_ID_B, sequences=(1,)).commands
        + make_snapshot(session_id=SESSION_ID, sequences=(1,)).commands,
    )
    ingest(journal, combined)
    claimed = SingleQueueScheduler(journal).claim_next()
    assert claimed.session_id == SESSION_ID
    journal.close()
