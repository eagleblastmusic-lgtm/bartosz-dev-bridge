from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, Journal
from bdb_bridge.migrations import JOURNAL_TABLES, MIGRATIONS, Migration, apply_migrations
from bdb_bridge.repository_index_migration import MIGRATION_V7, MIGRATION_V7_STATEMENTS

NOW = "2026-07-16T00:00:00Z"
V7_CHECKSUM = "639b9d4eaa0e142fc958c9fa0a1a03a2421802a75ba963b84c3b835d28e30cf8"


def test_v7_registry_and_literal_checksum() -> None:
    assert [m.version for m in MIGRATIONS] == list(range(1, 11))
    assert MIGRATIONS[6].name == "journal_v7_repository_index"
    assert MIGRATION_V7.checksum() == V7_CHECKSUM
    assert MIGRATION_V7.statements == MIGRATION_V7_STATEMENTS
    assert {
        "repository_snapshots",
        "repository_files",
        "repository_symbols",
    }.issubset(JOURNAL_TABLES)


def test_empty_db_and_reopen_apply_v7(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=lambda: NOW)
    for table in ("repository_snapshots", "repository_files", "repository_symbols"):
        assert journal._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() == (table,)
    assert journal._connection.execute(
        "SELECT version,name,checksum FROM schema_migrations WHERE version=7"
    ).fetchone() == (7, "journal_v7_repository_index", V7_CHECKSUM)
    journal.close()
    reopened = Journal.open(path, now_fn=lambda: NOW)
    assert reopened._connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=7").fetchone()[0] == 1
    reopened.close()


def _make_v6(path: Path, *, populated: bool) -> None:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn, tuple(m for m in MIGRATIONS if m.version <= 6), now_fn=lambda: NOW)
    if populated:
        session = "018f3f66-6cb3-4f66-9f2e-3d7647d1b707"
        state_hash = "sha256:" + "a" * 64
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
            (session, "repo", "a" * 40, "active", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?)",
            (session, str(path.parent / "worktrees" / session), "a" * 40, 1, state_hash, NOW, NOW),
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("populated", [False, True])
def test_v6_upgrade_preserves_data(tmp_path: Path, populated: bool) -> None:
    path = tmp_path / "v6.db"
    _make_v6(path, populated=populated)
    journal = Journal.open(path, now_fn=lambda: NOW)
    assert journal._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
    if populated:
        assert journal.get_workspace("018f3f66-6cb3-4f66-9f2e-3d7647d1b707") is not None
    journal.close()


def test_v7_statement_failure_rolls_back_only_v7(tmp_path: Path) -> None:
    path = tmp_path / "rollback.db"
    _make_v6(path, populated=False)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    broken = Migration(
        7,
        "journal_v7_repository_index",
        (
            MIGRATION_V7_STATEMENTS[0],
            "CREATE TABLE repository_snapshots (duplicate INTEGER)",
        ),
    )
    with pytest.raises(BridgeError):
        apply_migrations(conn, (*tuple(m for m in MIGRATIONS if m.version <= 6), broken), now_fn=lambda: NOW)
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='repository_snapshots'"
    ).fetchone() is None
    conn.close()


def test_future_version_rejected_after_v8(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    journal = Journal.open(path, now_fn=lambda: NOW)
    journal._connection.execute(
        "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(11,'future','x',?)",
        (NOW,),
    )
    journal._connection.commit()
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(path, now_fn=lambda: NOW)
    assert exc.value.code == "journal_schema_unsupported"


def test_no_public_delete_api_and_corruption_mapping(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "api.db", now_fn=lambda: NOW)
    assert not hasattr(journal, "delete_repository_snapshot")
    assert not hasattr(journal, "delete_repository_files")
    journal._connection.execute("PRAGMA ignore_check_constraints=ON")
    journal._connection.execute(
        """INSERT INTO repository_snapshots VALUES(
            'repo','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',?,?,?,?,?,?,?
        )""",
        ("bad-time", 0, 0, 0, 0, 0, "ghb1a-v1"),
    )
    journal._connection.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(BridgeError) as exc:
        journal.get_repository_snapshot("repo", "a" * 40)
    assert exc.value.code == "journal_corrupt"
    journal.close()
