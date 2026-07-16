from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, Journal
from bdb_bridge.code_relationship_migration import MIGRATION_V8, MIGRATION_V8_STATEMENTS
from bdb_bridge.migrations import JOURNAL_TABLES, MIGRATIONS, Migration, apply_migrations

NOW = "2026-07-16T01:30:00Z"
V8_CHECKSUM = "cbc8c9c6b5907c1f4d82cc9f95b095d8cceff4ef4aaca454f883cd3bb2ad55b6"


def test_v8_registry_checksum_and_tables() -> None:
    assert [item.version for item in MIGRATIONS] == list(range(1, 11))
    assert MIGRATIONS[7].name == "journal_v8_code_relationships"
    assert MIGRATION_V8.statements == MIGRATION_V8_STATEMENTS
    assert MIGRATION_V8.checksum() == V8_CHECKSUM
    assert {"repository_analyses", "repository_imports", "repository_symbol_references", "repository_dependency_edges"}.issubset(JOURNAL_TABLES)


def _make_v7(path: Path, *, populated: bool) -> None:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn, MIGRATIONS[:7], now_fn=lambda: NOW)
    if populated:
        conn.execute(
            """INSERT INTO repository_snapshots(
                repository_id,commit_sha,tree_sha,indexed_at,file_count,text_file_count,
                binary_file_count,python_file_count,symbol_count,indexer_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("repo", "a" * 40, "b" * 40, NOW, 0, 0, 0, 0, 0, "ghb1a-v1"),
        )
    conn.close()


@pytest.mark.parametrize("populated", [False, True])
def test_v7_upgrade_and_reopen(tmp_path: Path, populated: bool) -> None:
    path = tmp_path / "journal.db"
    _make_v7(path, populated=populated)
    journal = Journal.open(path, now_fn=lambda: NOW)
    assert journal._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
    if populated:
        assert journal.get_repository_snapshot("repo", "a" * 40) is not None
    journal.close()
    reopened = Journal.open(path, now_fn=lambda: NOW)
    assert reopened._connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=8").fetchone()[0] == 1
    reopened.close()


def test_v8_failure_rolls_back_and_future_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rollback.db"
    _make_v7(path, populated=False)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    broken = Migration(8, "journal_v8_code_relationships", (MIGRATION_V8_STATEMENTS[0], "CREATE TABLE repository_analyses (duplicate INTEGER)"))
    with pytest.raises(BridgeError):
        apply_migrations(conn, (*MIGRATIONS[:7], broken), now_fn=lambda: NOW)
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='repository_analyses'").fetchone() is None
    conn.close()
    future = tmp_path / "future.db"
    journal = Journal.open(future, now_fn=lambda: NOW)
    journal._connection.execute("INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(11,'future','x',?)", (NOW,))
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(future, now_fn=lambda: NOW)
    assert exc.value.code == "journal_schema_unsupported"


def test_no_public_delete_api_and_symbol_foreign_keys(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "api.db", now_fn=lambda: NOW)
    assert not hasattr(journal, "delete_repository_analysis")
    assert not hasattr(journal, "delete_repository_imports")
    foreign_targets = {row[2] for table in ("repository_imports", "repository_symbol_references", "repository_dependency_edges") for row in journal._connection.execute(f"PRAGMA foreign_key_list({table})")}
    assert "repository_symbols" in foreign_targets
    journal.close()
