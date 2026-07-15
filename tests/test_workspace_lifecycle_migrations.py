from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, Journal
from bdb_bridge.migrations import MIGRATIONS, Migration, apply_migrations
from bdb_bridge.workspace_lifecycle_migration import MIGRATION_V6, MIGRATION_V6_STATEMENTS

NOW = "2026-07-15T21:00:00Z"
SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b707"
HASH = "sha256:" + "a" * 64
V6_CHECKSUM = "44df37c558a1f315956f32ea4a4d865c239720d2f8d9471386b1fa8c9eb4fc97"


def test_v6_registry_and_literal_checksum() -> None:
    assert [m.version for m in MIGRATIONS] == [1, 2, 3, 4, 5, 6]
    assert [m.name for m in MIGRATIONS][-1] == "journal_v6_workspace_lifecycle"
    assert MIGRATION_V6.checksum() == V6_CHECKSUM
    assert MIGRATION_V6.statements == MIGRATION_V6_STATEMENTS


def test_empty_and_reopen_apply_v6(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=lambda: NOW)
    assert journal._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_lifecycle'"
    ).fetchone() == ("workspace_lifecycle",)
    assert journal._connection.execute(
        "SELECT version,name,checksum FROM schema_migrations WHERE version=6"
    ).fetchone() == (6, "journal_v6_workspace_lifecycle", V6_CHECKSUM)
    journal.close()
    reopened = Journal.open(path, now_fn=lambda: NOW)
    assert reopened._connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=6").fetchone()[0] == 1
    reopened.close()


def _make_v5(path: Path, *, populated: bool) -> None:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn, tuple(m for m in MIGRATIONS if m.version <= 5), now_fn=lambda: NOW)
    if populated:
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (SESSION, "repo", "a" * 40, "active", NOW, NOW))
        conn.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?)",
            (SESSION, str(path.parent / "worktrees" / SESSION), "a" * 40, 1, HASH, NOW, NOW),
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("populated", [False, True])
def test_v5_upgrade_to_v6_preserves_data(tmp_path: Path, populated: bool) -> None:
    path = tmp_path / "v5.db"
    _make_v5(path, populated=populated)
    journal = Journal.open(path, now_fn=lambda: NOW)
    assert journal._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
    if populated:
        assert journal.get_workspace(SESSION) is not None
    journal.close()


def test_v6_second_statement_failure_rolls_back_only_v6(tmp_path: Path) -> None:
    path = tmp_path / "rollback.db"
    _make_v5(path, populated=False)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    broken = Migration(6, "journal_v6_workspace_lifecycle", (
        MIGRATION_V6_STATEMENTS[0],
        "CREATE TABLE workspace_lifecycle (duplicate INTEGER)",
    ))
    with pytest.raises(BridgeError):
        apply_migrations(conn, (*tuple(m for m in MIGRATIONS if m.version <= 5), broken), now_fn=lambda: NOW)
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 5
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_lifecycle'"
    ).fetchone() is None
    conn.close()


def test_future_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    journal = Journal.open(path, now_fn=lambda: NOW)
    journal._connection.execute(
        "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(7,'future','x',?)", (NOW,)
    )
    journal._connection.commit()
    journal.close()
    with pytest.raises(BridgeError) as exc:
        Journal.open(path, now_fn=lambda: NOW)
    assert exc.value.code == "journal_schema_unsupported"


def test_constraints_one_row_and_no_delete_api(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "constraints.db", now_fn=lambda: NOW)
    journal._connection.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (SESSION, "repo", "a" * 40, "active", NOW, NOW))
    values = (SESSION, str(tmp_path / SESSION), "a" * 40, 0, HASH, "preserve", "preserved", None, None, None, None, NOW, NOW)
    journal._connection.execute("INSERT INTO workspace_lifecycle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    with pytest.raises(sqlite3.IntegrityError):
        journal._connection.execute("INSERT INTO workspace_lifecycle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    assert not hasattr(journal, "delete_workspace_lifecycle")
    journal.close()


def test_corrupted_lifecycle_row_maps_to_journal_corrupt(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "corrupt.db", now_fn=lambda: NOW)
    journal._connection.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (SESSION, "repo", "a" * 40, "active", NOW, NOW))
    journal._connection.execute(
        "INSERT INTO workspace_lifecycle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SESSION, str(tmp_path / SESSION), "a" * 40, 0, HASH, "preserve", "preserved", None, None, None, None, "bad-time", NOW),
    )
    with pytest.raises(BridgeError) as exc:
        journal.get_workspace_lifecycle(SESSION)
    assert exc.value.code == "journal_corrupt"
    journal.close()
