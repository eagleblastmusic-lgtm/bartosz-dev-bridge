from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bdb_bridge.journal import Journal
from bdb_bridge.migrations import MIGRATION_V1_STATEMENTS, MIGRATION_V2_STATEMENTS, MIGRATIONS, Migration, apply_migrations, utc_now_iso
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.protocol import BridgeError

FIXED_NOW = "2026-07-15T05:40:00Z"


def fixed_now() -> str:
    return FIXED_NOW


def open_db(tmp_path: Path, name: str = "journal.db") -> Journal:
    return Journal.open(tmp_path / name, now_fn=fixed_now)


def test_empty_db_applies_all_migrations(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        tables = {
            row[0]
            for row in journal._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert tables == {
            "schema_migrations",
            "sessions",
            "commands",
            "workspaces",
            "results",
            "events",
            "ingestion_sources",
            "session_ingestion",
            "command_ingestion",
            "ingestion_issues",
            "pending_command_documents",
            "operation_plans",
            "operation_effects",
            "outbox",
        }
        migrations = journal._connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert migrations == [
            (1, "journal_v1_initial"),
            (2, "journal_v2_ingestion"),
            (3, "journal_v3_execution"),
            (4, "journal_v4_result_outbox"),
        ]
    finally:
        journal.close()


def test_schema_has_foreign_keys_and_indexes(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        command_fks = journal._connection.execute("PRAGMA foreign_key_list(commands)").fetchall()
        assert any(row[2] == "sessions" for row in command_fks)

        indexes = {
            row[1]
            for row in journal._connection.execute(
                "SELECT type, name FROM sqlite_master WHERE tbl_name = 'events'"
            ).fetchall()
        }
        assert "idx_events_session" in indexes
        assert "idx_events_command" in indexes
        assert "events_no_update" in indexes
        assert "events_no_delete" in indexes
    finally:
        journal.close()


def test_reopen_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)
    journal.close()
    journal = Journal.open(path, now_fn=fixed_now)
    try:
        rows = journal._connection.execute("SELECT version FROM schema_migrations").fetchall()
        assert rows == [(1,), (2,), (3,), (4,)]
    finally:
        journal.close()


def test_migrations_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)
    journal.migrate()
    journal.migrate()
    journal.close()


def test_migration_uses_individual_execute_not_executescript() -> None:
    import inspect

    from bdb_bridge import migrations

    source = inspect.getsource(migrations.apply_migrations)
    assert "executescript" not in source
    for migration in MIGRATIONS:
        assert isinstance(migration.statements, tuple)
        assert migration.statements


def test_migration_rollback_on_failure(tmp_path: Path) -> None:
    failing = (
        Migration(1, "broken", ("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)", "CREATE TABLE broken_table (id INTEGER PRIMARY KEY)", "CREATE TABLE broken_table (duplicate INTEGER PRIMARY KEY)")),
    )
    conn = sqlite3.connect(tmp_path / "broken.db", timeout=5.0, isolation_level=None)
    with pytest.raises(BridgeError) as exc:
        apply_migrations(conn, failing, now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_CORRUPT
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert tables == set()
    conn.close()


def test_checksum_mismatch_detected(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    journal._connection.execute(
        "UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 1"
    )
    journal._connection.commit()
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH


def test_name_mismatch_detected(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    journal._connection.execute(
        "UPDATE schema_migrations SET name = 'renamed' WHERE version = 1"
    )
    journal._connection.commit()
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH


def test_future_schema_version_rejected(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    journal._connection.execute(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (5, 'future', 'abc', ?)",
        (FIXED_NOW,),
    )
    journal._connection.commit()
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_SCHEMA_UNSUPPORTED


def test_migration_gap_rejected(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    journal._connection.execute("DELETE FROM schema_migrations WHERE version >= 2")
    journal._connection.execute(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (3, 'gap', 'abc', ?)",
        (FIXED_NOW,),
    )
    journal._connection.commit()
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH


def test_corrupt_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not-a-sqlite-database")
    with pytest.raises(BridgeError) as exc:
        Journal.open(path, now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_CORRUPT


def test_unknown_tables_without_schema_migrations_rejected(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "foreign.db", timeout=5.0, isolation_level=None)
    conn.execute("CREATE TABLE user_owned (id INTEGER PRIMARY KEY)")
    conn.commit()
    with pytest.raises(BridgeError) as exc:
        apply_migrations(conn, now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_CORRUPT
    conn.close()


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        value = journal._connection.execute("PRAGMA foreign_keys").fetchone()
        assert value is not None and value[0] == 1
    finally:
        journal.close()


def test_journal_mode_wal(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        value = journal._connection.execute("PRAGMA journal_mode").fetchone()
        assert value is not None and str(value[0]).lower() == "wal"
    finally:
        journal.close()


def test_busy_timeout_is_5000(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        value = journal._connection.execute("PRAGMA busy_timeout").fetchone()
        assert value is not None and int(value[0]) == 5000
    finally:
        journal.close()


def test_synchronous_is_normal(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        value = journal._connection.execute("PRAGMA synchronous").fetchone()
        assert value is not None and int(value[0]) == 1
    finally:
        journal.close()


def test_concurrent_open_empty_db_creates_schema_once(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def opener() -> None:
        try:
            barrier.wait(timeout=5)
            for attempt in range(20):
                try:
                    journal = Journal.open(path, now_fn=fixed_now)
                    journal.close()
                    return
                except BridgeError as exc:
                    if "locked" in str(exc).lower() and attempt < 19:
                        time.sleep(0.25)
                        continue
                    raise
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=opener), threading.Thread(target=opener)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    conn = sqlite3.connect(path)
    versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    conn.close()
    assert versions == [(1,), (2,), (3,), (4,)]


def test_journal_open_closes_connection_on_migration_failure(tmp_path: Path) -> None:
    path = tmp_path / "fail.db"
    with patch("bdb_bridge.journal.apply_migrations", side_effect=BridgeError("journal_corrupt", "boom")):
        with pytest.raises(BridgeError):
            Journal.open(path, now_fn=fixed_now)
    conn = sqlite3.connect(path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    conn.close()
    assert tables == []


def test_migration_registry_rejects_duplicate_versions() -> None:
    broken = (
        Migration(1, "one", ("CREATE TABLE t1 (id INTEGER PRIMARY KEY)",)),
        Migration(1, "one_again", ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)",)),
    )
    with pytest.raises(ValueError):
        apply_migrations(sqlite3.connect(":memory:", isolation_level=None), broken, now_fn=fixed_now)


def test_migration_checksum_matches_statements() -> None:
    migration = MIGRATIONS[0]
    assert len(migration.checksum()) == 64


def test_v1_checksum_matches_golden() -> None:
    assert MIGRATIONS[0].checksum() == "1d293179f582464fa10eecd37fb381c0a5913d85ed629c9ec244c8bfdb2fe31a"


def test_upgrade_existing_v1_database_to_v2(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    apply_migrations(conn, (MIGRATIONS[0],), now_fn=fixed_now)
    conn.close()

    journal = Journal.open(path, now_fn=fixed_now)
    try:
        versions = journal._connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [
            (1, "journal_v1_initial"),
            (2, "journal_v2_ingestion"),
            (3, "journal_v3_execution"),
            (4, "journal_v4_result_outbox"),
        ]
        assert journal._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ingestion_sources'"
        ).fetchone() is not None
    finally:
        journal.close()


def test_upgrade_existing_v1_database_to_v2_with_data(tmp_path: Path) -> None:
    path = tmp_path / "v1_data.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)

    # 1. Apply v1
    apply_migrations(conn, (MIGRATIONS[0],), now_fn=fixed_now)

    # 2. Insert real v1 data: sessions, commands, workspaces, results, events
    now = FIXED_NOW
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("s1", "repo1", "a" * 40, "created", now, now)
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("s2", "repo1", "b" * 40, "active", now, now)
    )
    conn.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("c1", "s2", 1, "sha256:" + "a" * 64, '{"op":"test"}', "c" * 40, "claimed", 0, "hash1", now, now)
    )
    conn.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("c2", "s2", 2, "sha256:" + "b" * 64, '{"op":"test2"}', "d" * 40, "discovered", 0, "hash2", now, now)
    )
    conn.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("s2", "/path/to/ws", "b" * 40, 1, "hashws", now, now)
    )
    conn.execute(
        "INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("c1", "s2", 1, "success", None, "sha256:" + "r" * 64, "{}", "remote/path", now)
    )
    conn.execute(
        "INSERT INTO events (session_id, command_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        ("s2", "c1", "test_event", '{"foo":"bar"}', now)
    )

    # Check current state in v1
    v1_sessions = conn.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()
    v1_commands = conn.execute("SELECT * FROM commands ORDER BY command_id").fetchall()
    v1_workspaces = conn.execute("SELECT * FROM workspaces").fetchall()
    v1_results = conn.execute("SELECT * FROM results").fetchall()
    v1_events = conn.execute("SELECT session_id, command_id, event_type, payload_json, created_at FROM events").fetchall()

    conn.close()

    # 3. Apply remaining migrations
    journal = Journal.open(path, now_fn=fixed_now)
    try:
        versions = journal._connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [
            (1, "journal_v1_initial"),
            (2, "journal_v2_ingestion"),
            (3, "journal_v3_execution"),
            (4, "journal_v4_result_outbox"),
        ]

        v2_sessions = journal._connection.execute("SELECT session_id, repository_id, base_sha, state, created_at, updated_at FROM sessions ORDER BY session_id").fetchall()
        assert v2_sessions == v1_sessions

        v2_commands = journal._connection.execute("SELECT command_id, session_id, sequence, command_sha256, command_json, command_commit_sha, state, expected_revision, expected_state_hash, created_at, updated_at FROM commands ORDER BY command_id").fetchall()
        assert v2_commands == v1_commands

        v2_workspaces = journal._connection.execute("SELECT session_id, workspace_path, base_sha, revision, state_hash, created_at, updated_at FROM workspaces").fetchall()
        assert v2_workspaces == v1_workspaces

        v2_results = journal._connection.execute("SELECT command_id, session_id, sequence, status, error_code, result_sha256, result_json, remote_path, created_at FROM results").fetchall()
        assert v2_results == v1_results

        v2_events = journal._connection.execute("SELECT session_id, command_id, event_type, payload_json, created_at FROM events").fetchall()
        assert v2_events == v1_events
    finally:
        journal.close()


def test_upgrade_rejects_two_active_sessions_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "v1_conflict_sessions.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    apply_migrations(conn, (MIGRATIONS[0],), now_fn=fixed_now)

    now = FIXED_NOW
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", ("s1", "repo1", "a" * 40, "active", now, now))
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", ("s2", "repo1", "b" * 40, "completing", now, now))

    v1_sessions = conn.execute("SELECT * FROM sessions").fetchall()

    with pytest.raises(BridgeError) as exc:
        apply_migrations(conn, MIGRATIONS, now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH
    assert not conn.in_transaction

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "ingestion_sources" not in tables
    assert "pending_command_documents" not in tables

    versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert versions == [(1,)]

    v1_sessions_after = conn.execute("SELECT * FROM sessions").fetchall()
    assert v1_sessions_after == v1_sessions
    conn.close()


def test_upgrade_rejects_two_active_workers_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "v1_conflict_workers.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    apply_migrations(conn, (MIGRATIONS[0],), now_fn=fixed_now)

    now = FIXED_NOW
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", ("s1", "repo1", "a" * 40, "active", now, now))
    conn.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("c1", "s1", 1, "sha256:" + "a" * 64, '{}', "c" * 40, "executing", 0, "hash1", now, now))
    conn.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("c2", "s1", 2, "sha256:" + "b" * 64, '{}', "d" * 40, "claimed", 0, "hash2", now, now))

    v1_commands = conn.execute("SELECT * FROM commands").fetchall()

    with pytest.raises(BridgeError) as exc:
        apply_migrations(conn, MIGRATIONS, now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH
    assert not conn.in_transaction

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "ingestion_sources" not in tables

    versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert versions == [(1,)]

    v1_commands_after = conn.execute("SELECT * FROM commands").fetchall()
    assert v1_commands_after == v1_commands
    conn.close()


def test_v2_partial_unique_indexes_exist(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        indexes = {
            row[1]
            for row in journal._connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_sessions_one_active" in indexes
        assert "idx_commands_one_worker" in indexes
    finally:
        journal.close()


def test_v2_blocks_two_active_sessions(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        now = FIXED_NOW
        journal._connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "repo", "a" * 40, "created", now, now),
        )
        journal._connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            ("s2", "repo", "b" * 40, "created", now, now),
        )
        journal._connection.execute(
            "UPDATE sessions SET state = 'active', updated_at = ? WHERE session_id = 's1'",
            (now,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            journal._connection.execute(
                "UPDATE sessions SET state = 'active', updated_at = ? WHERE session_id = 's2'",
                (now,),
            )
    finally:
        journal.close()


def test_v2_blocks_two_active_workers(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    try:
        now = FIXED_NOW
        session = "018f3f66-6cb3-4f66-9f2e-3d7647d1b799"
        journal.create_session(session, "repo", "a" * 40)
        journal.record_command(
            session,
            f"{session}:000001",
            1,
            {
                "schema_version": "1.1",
                "session_id": session,
                "command_id": f"{session}:000001",
                "sequence": 1,
                "operation": "open_read",
                "expected_revision": 0,
                "payload": {},
            },
        )
        journal.record_command(
            session,
            f"{session}:000002",
            2,
            {
                "schema_version": "1.1",
                "session_id": session,
                "command_id": f"{session}:000002",
                "sequence": 2,
                "operation": "open_read",
                "expected_revision": 0,
                "payload": {},
            },
        )
        journal._connection.execute(
            "UPDATE commands SET state = 'claimed' WHERE command_id = ?",
            (f"{session}:000001",),
        )
        with pytest.raises(sqlite3.IntegrityError):
            journal._connection.execute(
                "UPDATE commands SET state = 'claimed' WHERE command_id = ?",
                (f"{session}:000002",),
            )
    finally:
        journal.close()


def test_v2_migration_rollback_leaves_no_partial_schema(tmp_path: Path) -> None:
    broken_v2 = (
        MIGRATIONS[0],
        Migration(
            2,
            "broken_v2",
            MIGRATION_V2_STATEMENTS[:3]
            + ("CREATE TABLE ingestion_sources (source_id TEXT PRIMARY KEY)",),
        ),
    )
    conn = sqlite3.connect(tmp_path / "rollback.db", timeout=5.0, isolation_level=None)
    with pytest.raises(BridgeError):
        apply_migrations(conn, broken_v2, now_fn=fixed_now)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert tables == set()
    conn.close()
