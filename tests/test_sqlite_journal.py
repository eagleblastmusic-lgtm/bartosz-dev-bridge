from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    BridgeErrorCode,
    CommandState,
    Journal,
    ResultStatus,
    SessionState,
    canonical_json,
    result_path_for,
)

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
COMMAND_ID = "cmd-00000001-0000-4000-8000-000000000001"
BASE_SHA = "a" * 40
FIXED_NOW = "2026-07-15T05:40:00Z"


def fixed_now() -> str:
    return FIXED_NOW


def open_journal(tmp_path: Path, name: str = "journal.db") -> Journal:
    return Journal.open(tmp_path / name, now_fn=fixed_now)


def sample_command(*, sequence: int = 1, command_id: str = COMMAND_ID) -> dict:
    return {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": command_id,
        "sequence": sequence,
        "operation": "open_read",
        "expected_revision": 0,
        "payload": {"path": "src/clamp.py"},
    }


def setup_session(journal: Journal) -> None:
    journal.create_session(SESSION_ID, "bdb-poc-fixture", BASE_SHA)


def setup_command(journal: Journal, *, sequence: int = 1, command_id: str = COMMAND_ID) -> None:
    setup_session(journal)
    journal.record_command(SESSION_ID, command_id, sequence, sample_command(sequence=sequence, command_id=command_id))


def sample_result_json(*, status: str = "success", sequence: int = 1, command_id: str = COMMAND_ID) -> str:
    payload = {
        "schema_version": "1.1",
        "session_id": SESSION_ID,
        "command_id": command_id,
        "sequence": sequence,
        "status": status,
        "summary": "ok",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def test_create_and_get_session(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        created = journal.create_session(SESSION_ID, "bdb-poc-fixture", BASE_SHA)
        assert created.state == SessionState.CREATED
        loaded = journal.get_session(SESSION_ID)
        assert loaded == created
    finally:
        journal.close()


def test_session_transition_allowed(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        updated = journal.transition_session(SESSION_ID, SessionState.CREATED, SessionState.ACTIVE)
        assert updated.state == SessionState.ACTIVE
    finally:
        journal.close()


def test_session_transition_invalid_graph(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        with pytest.raises(BridgeError) as exc:
            journal.transition_session(SESSION_ID, SessionState.CREATED, SessionState.COMPLETED)
        assert exc.value.code == BridgeErrorCode.INVALID_STATE_TRANSITION
        assert journal._connection.in_transaction is False
    finally:
        journal.close()


def test_session_transition_stale_expected_state(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        with pytest.raises(BridgeError) as exc:
            journal.transition_session(SESSION_ID, SessionState.ACTIVE, SessionState.COMPLETING)
        assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT
    finally:
        journal.close()


def test_session_transition_missing_record(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        with pytest.raises(BridgeError) as exc:
            journal.transition_session(SESSION_ID, SessionState.CREATED, SessionState.ACTIVE)
        assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT
    finally:
        journal.close()


def test_record_command_idempotent_insert(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        first = journal.record_command(SESSION_ID, COMMAND_ID, 1, sample_command())
        second = journal.record_command(SESSION_ID, COMMAND_ID, 1, sample_command())
        assert second == first
        assert len(journal.list_events(session_id=SESSION_ID, command_id=COMMAND_ID)) == 1
    finally:
        journal.close()


def test_record_command_idempotent_after_state_change(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        journal.transition_command(COMMAND_ID, CommandState.DISCOVERED, CommandState.VALIDATED)
        replay = journal.record_command(SESSION_ID, COMMAND_ID, 1, sample_command())
        assert replay.state == CommandState.VALIDATED
    finally:
        journal.close()


def test_record_command_id_collision(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal.record_command(SESSION_ID, COMMAND_ID, 1, sample_command())
        mutated = sample_command()
        mutated["payload"] = {"path": "tests/test_clamp.py"}
        with pytest.raises(BridgeError) as exc:
            journal.record_command(SESSION_ID, COMMAND_ID, 1, mutated)
        assert exc.value.code == BridgeErrorCode.COMMAND_ID_COLLISION
    finally:
        journal.close()


def test_record_command_sequence_collision(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal.record_command(SESSION_ID, COMMAND_ID, 1, sample_command(sequence=1))
        other_id = "cmd-00000002-0000-4000-8000-000000000002"
        with pytest.raises(BridgeError) as exc:
            journal.record_command(SESSION_ID, other_id, 1, sample_command(sequence=1, command_id=other_id))
        assert exc.value.code == BridgeErrorCode.SEQUENCE_COLLISION
    finally:
        journal.close()


def test_command_transition_allowed(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        updated = journal.transition_command(COMMAND_ID, CommandState.DISCOVERED, CommandState.VALIDATED)
        assert updated.state == CommandState.VALIDATED
    finally:
        journal.close()


def test_command_transition_invalid_graph(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        with pytest.raises(BridgeError) as exc:
            journal.transition_command(COMMAND_ID, CommandState.DISCOVERED, CommandState.CLAIMED)
        assert exc.value.code == BridgeErrorCode.INVALID_STATE_TRANSITION
        assert journal._connection.in_transaction is False
    finally:
        journal.close()


def test_reopen_preserves_records(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    journal = Journal.open(path, now_fn=fixed_now)
    setup_command(journal)
    journal.close()

    journal = Journal.open(path, now_fn=fixed_now)
    try:
        assert journal.get_session(SESSION_ID) is not None
        assert journal.get_command(COMMAND_ID) is not None
    finally:
        journal.close()


def test_register_and_get_workspace(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        created = journal.register_workspace(
            SESSION_ID,
            str(tmp_path / "worktrees" / SESSION_ID),
            BASE_SHA,
            0,
            "sha256:" + "b" * 64,
        )
        assert journal.get_workspace(SESSION_ID) == created
    finally:
        journal.close()


def test_workspace_compare_and_swap_success(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal.register_workspace(SESSION_ID, str(tmp_path / "ws"), BASE_SHA, 0, "sha256:" + "b" * 64)
        updated = journal.update_workspace_state(
            SESSION_ID,
            0,
            "sha256:" + "b" * 64,
            1,
            "sha256:" + "c" * 64,
        )
        assert updated.revision == 1
        assert updated.state_hash == "sha256:" + "c" * 64
    finally:
        journal.close()


def test_workspace_stale_revision(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal.register_workspace(SESSION_ID, str(tmp_path / "ws"), BASE_SHA, 0, "sha256:" + "b" * 64)
        with pytest.raises(BridgeError) as exc:
            journal.update_workspace_state(
                SESSION_ID,
                1,
                "sha256:" + "b" * 64,
                2,
                "sha256:" + "c" * 64,
            )
        assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT
    finally:
        journal.close()


def test_workspace_state_hash_mismatch(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal.register_workspace(SESSION_ID, str(tmp_path / "ws"), BASE_SHA, 0, "sha256:" + "b" * 64)
        with pytest.raises(BridgeError) as exc:
            journal.update_workspace_state(
                SESSION_ID,
                0,
                "sha256:" + "d" * 64,
                1,
                "sha256:" + "c" * 64,
            )
        assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT
    finally:
        journal.close()


def test_workspace_revision_must_advance_by_one(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal.register_workspace(SESSION_ID, str(tmp_path / "ws"), BASE_SHA, 0, "sha256:" + "b" * 64)
        with pytest.raises(BridgeError) as exc:
            journal.update_workspace_state(
                SESSION_ID,
                0,
                "sha256:" + "b" * 64,
                2,
                "sha256:" + "c" * 64,
            )
        assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT
    finally:
        journal.close()


def test_rollback_leaves_no_partial_records(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        with pytest.raises(BridgeError):
            journal.transition_session(SESSION_ID, SessionState.CREATED, SessionState.COMPLETED)
        session = journal.get_session(SESSION_ID)
        assert session is not None and session.state == SessionState.CREATED
        assert not any(
            event.event_type == "session.state_changed"
            for event in journal.list_events(session_id=SESSION_ID)
        )
        assert journal._connection.in_transaction is False
    finally:
        journal.close()


def test_store_result_idempotent(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        result_json = sample_result_json()
        remote_path = result_path_for(SESSION_ID, 1)
        first = journal.store_result(COMMAND_ID, result_json, remote_path)
        second = journal.store_result(COMMAND_ID, result_json, remote_path)
        assert second == first
        assert journal.get_result(COMMAND_ID).result_json == result_json
    finally:
        journal.close()


def test_store_result_collision(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        remote_path = result_path_for(SESSION_ID, 1)
        journal.store_result(COMMAND_ID, sample_result_json(status="success"), remote_path)
        with pytest.raises(BridgeError) as exc:
            journal.store_result(COMMAND_ID, sample_result_json(status="failed"), remote_path)
        assert exc.value.code == BridgeErrorCode.RESULT_COLLISION
    finally:
        journal.close()


def test_store_result_sequence_collision(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal.record_command(SESSION_ID, COMMAND_ID, 1, sample_command(sequence=1))
        other_id = "cmd-00000002-0000-4000-8000-000000000002"
        journal.record_command(SESSION_ID, other_id, 2, sample_command(sequence=2, command_id=other_id))
        journal._connection.execute(
            """
            INSERT INTO results (
                command_id, session_id, sequence, status, error_code,
                result_sha256, result_json, remote_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                other_id,
                SESSION_ID,
                1,
                "failed",
                None,
                "sha256:" + "0" * 64,
                sample_result_json(sequence=1, command_id=other_id, status="failed"),
                result_path_for(SESSION_ID, 1),
                FIXED_NOW,
            ),
        )
        journal._connection.commit()
        with pytest.raises(BridgeError) as exc:
            journal.store_result(
                COMMAND_ID,
                sample_result_json(sequence=1, command_id=COMMAND_ID),
                result_path_for(SESSION_ID, 1),
            )
        assert exc.value.code == BridgeErrorCode.RESULT_COLLISION
    finally:
        journal.close()


def test_store_result_metadata_mismatch(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        bad_json = sample_result_json()
        parsed = json.loads(bad_json)
        parsed["sequence"] = 99
        with pytest.raises(BridgeError) as exc:
            journal.store_result(COMMAND_ID, json.dumps(parsed), result_path_for(SESSION_ID, 1))
        assert exc.value.code == BridgeErrorCode.INVALID_PAYLOAD
    finally:
        journal.close()


def test_store_result_preserves_bytes(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        result_json = sample_result_json() + " "
        remote_path = result_path_for(SESSION_ID, 1)
        journal.store_result(COMMAND_ID, result_json, remote_path)
        stored = journal.get_result(COMMAND_ID)
        assert stored is not None
        assert stored.result_json == result_json
    finally:
        journal.close()


def test_store_result_unknown_status(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        with pytest.raises(BridgeError) as exc:
            journal.store_result(
                COMMAND_ID,
                sample_result_json(status="totally_unknown"),
                result_path_for(SESSION_ID, 1),
            )
        assert exc.value.code == BridgeErrorCode.INVALID_PAYLOAD
    finally:
        journal.close()


def test_store_result_bridge_error_status_sets_error_code(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        stored = journal.store_result(
            COMMAND_ID,
            sample_result_json(status="stale_revision"),
            result_path_for(SESSION_ID, 1),
        )
        assert stored.status == "stale_revision"
        assert stored.error_code == "stale_revision"
    finally:
        journal.close()


def test_store_result_success_status_has_no_error_code(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        stored = journal.store_result(
            COMMAND_ID,
            sample_result_json(status=ResultStatus.SUCCESS),
            result_path_for(SESSION_ID, 1),
        )
        assert stored.error_code is None
    finally:
        journal.close()


def test_store_result_too_large(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        huge = sample_result_json()
        parsed = json.loads(huge)
        parsed["summary"] = "x" * 20_000
        with pytest.raises(BridgeError) as exc:
            journal.store_result(COMMAND_ID, json.dumps(parsed), result_path_for(SESSION_ID, 1))
        assert exc.value.code == BridgeErrorCode.RESULT_TOO_LARGE
    finally:
        journal.close()


def test_append_only_events_and_triggers(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        event = journal.append_event(session_id=SESSION_ID, event_type="note", payload={"k": "v"})
        events = [item for item in journal.list_events(session_id=SESSION_ID) if item.event_type == "note"]
        assert events == [event]
        with pytest.raises(sqlite3.IntegrityError):
            journal._connection.execute("UPDATE events SET event_type = 'x' WHERE event_id = ?", (event.event_id,))
        with pytest.raises(sqlite3.IntegrityError):
            journal._connection.execute("DELETE FROM events WHERE event_id = ?", (event.event_id,))
    finally:
        journal.close()


def test_transition_writes_event_atomically(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        journal.transition_command(COMMAND_ID, CommandState.DISCOVERED, CommandState.VALIDATED)
        events = journal.list_events(command_id=COMMAND_ID)
        assert any(event.event_type == "command.state_changed" for event in events)
    finally:
        journal.close()


def test_foreign_key_violation_on_command_without_session(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        with pytest.raises(BridgeError) as exc:
            journal.record_command(SESSION_ID, COMMAND_ID, 1, sample_command())
        assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT
    finally:
        journal.close()


def test_unknown_command_state_in_db_is_corrupt(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_command(journal)
        journal._connection.execute(
            "UPDATE commands SET state = 'not_a_real_state' WHERE command_id = ?",
            (COMMAND_ID,),
        )
        journal._connection.commit()
        with pytest.raises(BridgeError) as exc:
            journal.get_command(COMMAND_ID)
        assert exc.value.code == BridgeErrorCode.JOURNAL_CORRUPT
    finally:
        journal.close()


def test_unknown_session_state_in_db_is_corrupt(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        journal._connection.execute(
            "UPDATE sessions SET state = 'not_a_real_state' WHERE session_id = ?",
            (SESSION_ID,),
        )
        journal._connection.commit()
        with pytest.raises(BridgeError) as exc:
            journal.get_session(SESSION_ID)
        assert exc.value.code == BridgeErrorCode.JOURNAL_CORRUPT
    finally:
        journal.close()


def test_reader_does_not_see_uncommitted_writer_state(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    writer = Journal.open(path, now_fn=fixed_now)
    reader = Journal.open(path, now_fn=fixed_now)
    setup_session(writer)
    ready = threading.Barrier(2)
    read_done = threading.Event()
    seen: list[SessionState | None] = []
    error: list[Exception] = []

    def write() -> None:
        try:
            writer._connection.execute("BEGIN IMMEDIATE")
            writer._connection.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE session_id = ?",
                (SessionState.ACTIVE.value, FIXED_NOW, SESSION_ID),
            )
            ready.wait(timeout=5)
            read_done.wait(timeout=5)
            writer._connection.commit()
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    ready.wait(timeout=5)
    session = reader.get_session(SESSION_ID)
    seen.append(session.state if session else None)
    read_done.set()
    thread.join(timeout=5)
    writer.close()
    reader.close()
    assert not error
    assert seen == [SessionState.CREATED]


def test_two_connections_read_committed_state(tmp_path: Path) -> None:
    path = tmp_path / "committed.db"
    writer = Journal.open(path, now_fn=fixed_now)
    setup_session(writer)
    writer.transition_session(SESSION_ID, SessionState.CREATED, SessionState.ACTIVE)
    writer.close()

    reader = Journal.open(path, now_fn=fixed_now)
    try:
        session = reader.get_session(SESSION_ID)
        assert session is not None and session.state == SessionState.ACTIVE
    finally:
        reader.close()


def test_close_reopen_no_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "close.db"
    journal = Journal.open(path, now_fn=fixed_now)
    setup_command(journal)
    journal.register_workspace(SESSION_ID, str(tmp_path / "ws"), BASE_SHA, 0, "sha256:" + "b" * 64)
    result_json = sample_result_json()
    journal.store_result(COMMAND_ID, result_json, result_path_for(SESSION_ID, 1))
    journal.close()

    journal = Journal.open(path, now_fn=fixed_now)
    try:
        assert journal.get_command(COMMAND_ID) is not None
        assert journal.get_workspace(SESSION_ID) is not None
        assert journal.get_result(COMMAND_ID).result_json == result_json
        assert len(journal.list_events(session_id=SESSION_ID)) >= 4
    finally:
        journal.close()


def test_write_methods_after_close_raise_controlled_error(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    journal.close()
    with pytest.raises(BridgeError) as exc:
        journal.create_session(SESSION_ID, "repo", BASE_SHA)
    assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT


def test_invalid_sequence_bool_rejected(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        with pytest.raises(BridgeError) as exc:
            journal.record_command(SESSION_ID, COMMAND_ID, True, sample_command())  # type: ignore[arg-type]
        assert exc.value.code == BridgeErrorCode.INVALID_PAYLOAD
    finally:
        journal.close()


def test_command_json_identity_mismatch_rejected(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    try:
        setup_session(journal)
        command = sample_command()
        command["sequence"] = 2
        with pytest.raises(BridgeError) as exc:
            journal.record_command(SESSION_ID, COMMAND_ID, 1, command)
        assert exc.value.code == BridgeErrorCode.INVALID_PAYLOAD
    finally:
        journal.close()
