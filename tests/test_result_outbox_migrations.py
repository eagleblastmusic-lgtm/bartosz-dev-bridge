from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, Journal
from bdb_bridge.migrations import JOURNAL_TABLES, MIGRATIONS, Migration, apply_migrations

NOW = "2026-07-15T12:00:00Z"
V1 = "1d293179f582464fa10eecd37fb381c0a5913d85ed629c9ec244c8bfdb2fe31a"
V2 = "80178c2da604e77b9f568467ffa54865dbad3867193dc9f489e002cb5c3dbc33"
V3 = "4dffb2c3e5807cba98d8f5323554e625e4acc58559cc807e2728eab7f07bb9db"
V4 = "b19f7ef96b5c9e25ad9cad9c6d2160a667c5c1b5db68d1d0e7accb2f1f2ba3c9"


def now() -> str:
    return NOW


def test_v4_registry_and_literal_checksums() -> None:
    assert [(m.version, m.name, m.checksum()) for m in MIGRATIONS] == [
        (1, "journal_v1_initial", V1),
        (2, "journal_v2_ingestion", V2),
        (3, "journal_v3_execution", V3),
        (4, "journal_v4_result_outbox", V4),
    ]
    assert "outbox" in JOURNAL_TABLES


def test_empty_and_populated_v3_upgrade(tmp_path: Path) -> None:
    empty = Journal.open(tmp_path / "empty.db", now_fn=now)
    assert empty._connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,)]
    assert {r[0] for r in empty._connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} == JOURNAL_TABLES
    empty.close()

    path = tmp_path / "populated.db"
    conn = sqlite3.connect(path, isolation_level=None)
    apply_migrations(conn, MIGRATIONS[:3], now_fn=now)
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", ("s1", "repo", "a" * 40, "created", NOW, NOW))
    before = conn.execute("SELECT * FROM sessions").fetchall()
    conn.close()
    journal = Journal.open(path, now_fn=now)
    assert journal._connection.execute("SELECT * FROM sessions").fetchall() == before
    assert journal._connection.execute("SELECT COUNT(*) FROM outbox").fetchone() == (0,)
    indexes = {r[1] for r in journal._connection.execute("PRAGMA index_list(outbox)").fetchall()}
    assert "idx_outbox_due" in indexes
    journal.close()


def test_v4_second_statement_failure_rolls_back(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "rollback.db", isolation_level=None)
    apply_migrations(conn, MIGRATIONS[:3], now_fn=now)
    broken = Migration(4, "journal_v4_result_outbox", (MIGRATIONS[3].statements[0], "CREATE INDEX broken syntax"))
    with pytest.raises(BridgeError):
        apply_migrations(conn, (*MIGRATIONS[:3], broken), now_fn=now)
    assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,)]
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone() is None
    conn.close()


def test_outbox_constraints(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "constraints.db", now_fn=now)
    conn = journal._connection
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", ("s1", "repo", "a" * 40, "created", NOW, NOW))
    conn.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("c1", "s1", 1, "h", "{}", None, "result_staged", 0, None, NOW, NOW))
    conn.execute("INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("c1", "s1", 1, "success", None, "sha256:" + "a" * 64, "{}", "x.json", NOW))
    conn.execute("INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("c1", "s1", 1, "sha256:" + "a" * 64, "x.json", "pending", 0, None, None, None, None, NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("missing", "s1", 2, "sha256:" + "b" * 64, "y.json", "pending", 0, None, None, None, None, NOW, NOW))
    journal.close()
