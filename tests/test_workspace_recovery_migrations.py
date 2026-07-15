from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, BridgeErrorCode, Journal
from bdb_bridge.migrations import JOURNAL_TABLES, MIGRATIONS, Migration, apply_migrations

FIXED_NOW = "2026-07-15T12:00:00Z"
V1_CHECKSUM = "1d293179f582464fa10eecd37fb381c0a5913d85ed629c9ec244c8bfdb2fe31a"
V2_CHECKSUM = "80178c2da604e77b9f568467ffa54865dbad3867193dc9f489e002cb5c3dbc33"
V3_CHECKSUM = "4dffb2c3e5807cba98d8f5323554e625e4acc58559cc807e2728eab7f07bb9db"
V4_CHECKSUM = "b19f7ef96b5c9e25ad9cad9c6d2160a667c5c1b5db68d1d0e7accb2f1f2ba3c9"


def fixed_now() -> str:
    return FIXED_NOW


def test_ghb04_v1_v2_v3_literal_golden_checksums() -> None:
    assert [(m.version, m.name, m.checksum()) for m in MIGRATIONS] == [
        (1, "journal_v1_initial", V1_CHECKSUM),
        (2, "journal_v2_ingestion", V2_CHECKSUM),
        (3, "journal_v3_execution", V3_CHECKSUM),
        (4, "journal_v4_result_outbox", V4_CHECKSUM),
    ]


def test_ghb04_empty_and_populated_v2_upgrade_to_v3(tmp_path: Path) -> None:
    empty = Journal.open(tmp_path / "empty.db", now_fn=fixed_now)
    assert empty._connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,)]
    assert {r[0] for r in empty._connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} == JOURNAL_TABLES
    empty.close()

    path = tmp_path / "populated.db"
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    apply_migrations(conn, MIGRATIONS[:2], now_fn=fixed_now)
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", ("s1", "repo", "a" * 40, "active", FIXED_NOW, FIXED_NOW))
    conn.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("c1", "s1", 1, "hash", "{}", None, "claimed", 0, None, FIXED_NOW, FIXED_NOW))
    conn.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)", ("s1", "/ws", "a" * 40, 0, "state", FIXED_NOW, FIXED_NOW))
    before = [conn.execute(f"SELECT * FROM {table}").fetchall() for table in ("sessions", "commands", "workspaces")]
    conn.close()
    journal = Journal.open(path, now_fn=fixed_now)
    after = [journal._connection.execute(f"SELECT * FROM {table}").fetchall() for table in ("sessions", "commands", "workspaces")]
    assert after == before
    assert journal._connection.execute("SELECT COUNT(*) FROM operation_plans").fetchone()[0] == 0
    assert journal._connection.execute("SELECT COUNT(*) FROM operation_effects").fetchone()[0] == 0
    journal.close()


def test_ghb04_v3_second_statement_failure_rolls_back_all_v3(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "rollback.db", timeout=5.0, isolation_level=None)
    apply_migrations(conn, MIGRATIONS[:2], now_fn=fixed_now)
    broken = Migration(3, "journal_v3_execution", (MIGRATIONS[2].statements[0], "CREATE TABLE operation_effects (bad syntax"))
    with pytest.raises(BridgeError):
        apply_migrations(conn, (MIGRATIONS[0], MIGRATIONS[1], broken), now_fn=fixed_now)
    assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='operation_plans'").fetchone() is None
    conn.close()


def test_ghb04_v3_reopen_noop_checksum_mismatch_and_future_version(tmp_path: Path) -> None:
    path = tmp_path / "reopen.db"
    journal = Journal.open(path, now_fn=fixed_now)
    journal.close()
    journal = Journal.open(path, now_fn=fixed_now)
    journal.migrate()
    journal.close()

    journal = Journal.open(path, now_fn=fixed_now)
    journal._connection.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=3")
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(path, now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH

    future = tmp_path / "future.db"
    journal = Journal.open(future, now_fn=fixed_now)
    journal._connection.execute("INSERT INTO schema_migrations VALUES (5, 'future', 'x', ?)", (FIXED_NOW,))
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(future, now_fn=fixed_now)
    assert exc.value.code == BridgeErrorCode.JOURNAL_SCHEMA_UNSUPPORTED


def test_ghb04_migration_registry_is_exact() -> None:
    assert tuple(m.version for m in MIGRATIONS) == (1, 2, 3, 4)
    assert tuple(m.name for m in MIGRATIONS) == (
        "journal_v1_initial",
        "journal_v2_ingestion",
        "journal_v3_execution",
        "journal_v4_result_outbox",
    )
