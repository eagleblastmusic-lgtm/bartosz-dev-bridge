from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from bdb_bridge import BridgeError, BridgeErrorCode, Journal
from bdb_bridge.migrations import JOURNAL_TABLES, MIGRATIONS, Migration, apply_migrations

FIXED_NOW = "2026-07-15T12:00:00Z"
V5_CHECKSUM = "9bfc62c82e71ebbf968f6a171eb0b320a4d2510dec158db13a8d940afd315670"


def fixed_now() -> str:
    return FIXED_NOW


def test_v5_registry_and_checksum() -> None:
    # Verify migration version 5 is in registry and has correct checksum
    v5_mig = MIGRATIONS[4]
    assert v5_mig.version == 5
    assert v5_mig.name == "journal_v5_service_lifecycle"
    assert v5_mig.checksum() == V5_CHECKSUM


def test_v5_empty_upgrade(tmp_path: Path) -> None:
    # Successful upgrade from empty v4 to v5
    path = tmp_path / "empty_v5.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)

    # Apply up to v4 first
    apply_migrations(conn, MIGRATIONS[:4], now_fn=fixed_now)
    assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,)]

    # Verify service_instances table does not exist yet
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='service_instances'")
    assert cur.fetchone() is None

    # Run upgrade to v5 only; v6 is covered separately.
    apply_migrations(conn, MIGRATIONS[:5], now_fn=fixed_now)
    assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,), (5,)]

    # Verify table and constraints
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='service_instances'")
    assert cur.fetchone() is not None
    conn.close()


def test_v5_populated_upgrade(tmp_path: Path) -> None:
    # Successful upgrade from populated v4 database
    path = tmp_path / "populated_v5.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    apply_migrations(conn, MIGRATIONS[:4], now_fn=fixed_now)

    # Populate some v4 data
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", ("s1", "repo", "a" * 40, "active", FIXED_NOW, FIXED_NOW))
    conn.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("c1", "s1", 1, "hash", "{}", None, "claimed", 0, None, FIXED_NOW, FIXED_NOW))

    conn.close()

    # Open through Journal to trigger all current migrations, including all current migrations.
    journal = Journal.open(path, now_fn=fixed_now)

    # Verify data is still intact
    assert journal._conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "s1"
    assert journal._conn.execute("SELECT command_id FROM commands").fetchone()[0] == "c1"
    assert journal._conn.execute("SELECT COUNT(*) FROM service_instances").fetchone()[0] == 0
    assert journal._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 8
    journal.close()


def test_v5_migration_error_rollback(tmp_path: Path) -> None:
    # Rollback when v5 statements encounter a failure
    path = tmp_path / "rollback_v5.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    apply_migrations(conn, MIGRATIONS[:4], now_fn=fixed_now)

    # Make a broken version 5 migration
    broken = Migration(5, "journal_v5_service_lifecycle", (MIGRATIONS[4].statements[0], "CREATE UNIQUE INDEX bad syntax (("))

    with pytest.raises(BridgeError):
        apply_migrations(conn, (MIGRATIONS[0], MIGRATIONS[1], MIGRATIONS[2], MIGRATIONS[3], broken), now_fn=fixed_now)

    # Check that schema version is still 4 and service_instances is not created/rolled back
    assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,)]
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='service_instances'")
    assert cur.fetchone() is None
    conn.close()


def test_v5_unique_active_index_constraint(tmp_path: Path) -> None:
    # Verify that unique active index prevents double start
    path = tmp_path / "constraint_v5.db"
    journal = Journal.open(path, now_fn=fixed_now)

    # Register active running instance
    journal.start_service_instance("inst-11111111-1111-1111-1111-111111111111", pid=100, started_at=FIXED_NOW)

    # Trying to insert another running instance violates unique index and throws BridgeError (JOURNAL_CONFLICT)
    with pytest.raises(BridgeError) as exc:
        journal.start_service_instance("inst-22222222-2222-2222-2222-222222222222", pid=200, started_at=FIXED_NOW)
    assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT

    # Stop the first instance
    journal.mark_service_instance_stopped("inst-11111111-1111-1111-1111-111111111111", exit_code=0)

    # Starting another active instance now succeeds
    inst2 = journal.start_service_instance("inst-22222222-2222-2222-2222-222222222222", pid=200, started_at=FIXED_NOW)
    assert inst2.instance_id == "inst-22222222-2222-2222-2222-222222222222"

    journal.close()
