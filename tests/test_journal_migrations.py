from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bdb_bridge.journal import Journal
from bdb_bridge.migrations import MIGRATIONS, Migration, apply_migrations, utc_now_iso
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
        }
        migrations = journal._connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert migrations == [(1, "journal_v1_initial")]
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
        assert rows == [(1,)]
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
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (2, 'future', 'abc', ?)",
        (FIXED_NOW,),
    )
    journal._connection.commit()
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_SCHEMA_UNSUPPORTED


def test_migration_gap_rejected(tmp_path: Path) -> None:
    journal = open_db(tmp_path)
    journal._connection.execute("DELETE FROM schema_migrations WHERE version = 1")
    journal._connection.execute(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (2, 'gap', 'abc', ?)",
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
    assert versions == [(1,)]


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
