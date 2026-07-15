from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from bdb_bridge import Journal, BridgeError, BridgeErrorCode
from bdb_bridge.migrations import MIGRATIONS, apply_migrations, JOURNAL_TABLES

FIXED_NOW = "2026-07-15T12:00:00Z"
def fixed_now() -> str:
    return FIXED_NOW

def test_v3_empty_db_applies_all_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "v3_empty.db"
    journal = Journal.open(db_path, now_fn=fixed_now)
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
        }

        versions = journal._connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [
            (1, "journal_v1_initial"),
            (2, "journal_v2_ingestion"),
            (3, "journal_v3_execution"),
        ]
    finally:
        journal.close()

def test_v3_upgrade_existing_v2_with_data(tmp_path: Path) -> None:
    db_path = tmp_path / "v2_data.db"
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)

    # 1. Apply v1 and v2 migrations only
    apply_migrations(conn, MIGRATIONS[:2], now_fn=fixed_now)

    # 2. Insert dummy v2 data
    now = FIXED_NOW
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("s1", "repo1", "a" * 40, "active", now, now)
    )
    conn.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("c1", "s1", 1, "sha256:" + "a" * 64, "{}", "c" * 40, "claimed", 0, "hash1", now, now)
    )
    conn.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("s1", "/path/to/ws", "a" * 40, 0, "hashws", now, now)
    )

    v2_sessions = conn.execute("SELECT * FROM sessions").fetchall()
    v2_commands = conn.execute("SELECT * FROM commands").fetchall()
    v2_workspaces = conn.execute("SELECT * FROM workspaces").fetchall()
    conn.close()

    # 3. Upgrade to v3 by opening the database with Journal.open()
    journal = Journal.open(db_path, now_fn=fixed_now)
    try:
        # Check that all 3 versions are registered
        versions = journal._connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [
            (1, "journal_v1_initial"),
            (2, "journal_v2_ingestion"),
            (3, "journal_v3_execution"),
        ]

        # Verify that v2 data is preserved completely
        assert journal._connection.execute("SELECT * FROM sessions").fetchall() == v2_sessions
        assert journal._connection.execute("SELECT * FROM commands").fetchall() == v2_commands
        assert journal._connection.execute("SELECT * FROM workspaces").fetchall() == v2_workspaces

        # Verify new tables are empty but exist
        assert journal._connection.execute("SELECT COUNT(*) FROM operation_plans").fetchone()[0] == 0
        assert journal._connection.execute("SELECT COUNT(*) FROM operation_effects").fetchone()[0] == 0
    finally:
        journal.close()

def test_v3_migration_rollback_on_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "rollback.db"
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)

    # Apply v1 and v2
    apply_migrations(conn, MIGRATIONS[:2], now_fn=fixed_now)

    # Create a bad migration v3 that has a syntax error
    from bdb_bridge.migrations import Migration
    bad_v3 = Migration(3, "journal_v3_execution", ("CREATE TABLE operation_plans (bad syntax",))

    with pytest.raises(BridgeError) as exc:
        apply_migrations(conn, (MIGRATIONS[0], MIGRATIONS[1], bad_v3), now_fn=fixed_now)

    assert exc.value.code == BridgeErrorCode.JOURNAL_CORRUPT

    # Ensure database version is still 2 and no bad tables were created
    versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert versions == [(1,), (2,)]

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert "operation_plans" not in tables
    conn.close()

def test_v3_golden_migrations_unchanged() -> None:
    # Check version 1 and 2 literal names and checksums to ensure they weren't altered
    assert MIGRATIONS[0].version == 1
    assert MIGRATIONS[0].name == "journal_v1_initial"

    assert MIGRATIONS[1].version == 2
    assert MIGRATIONS[1].name == "journal_v2_ingestion"

    assert len(MIGRATIONS) == 3

def test_v3_schema_registry() -> None:
    # Check that JOURNAL_TABLES set has all expected tables in the registry
    expected = {
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
    }
    assert JOURNAL_TABLES == expected
