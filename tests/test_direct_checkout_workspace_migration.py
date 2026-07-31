from __future__ import annotations

import sqlite3
from pathlib import Path

from bdb_bridge import Journal
from bdb_bridge.direct_checkout_workspace_migration import MIGRATION_V11


SESSION_1 = "11000000-0000-4000-8000-000000000001"
SESSION_2 = "11000000-0000-4000-8000-000000000002"
BASE_SHA = "1" * 40
STATE_HASH = "sha256:" + ("2" * 64)
EXPECTED_CHECKSUM = "178a97cf4ebc1e879964b3b77a7650d994487b92f4d95c3b4541793a92ca921c"


def unique_index_columns(connection: sqlite3.Connection, table: str) -> list[list[str]]:
    results: list[list[str]] = []
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if int(row[2]) != 1:
            continue
        index_name = str(row[1])
        columns = [
            str(item[2])
            for item in connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        ]
        results.append(columns)
    return results


def test_v11_literal_checksum() -> None:
    assert MIGRATION_V11.version == 11
    assert MIGRATION_V11.name == "journal_v11_shared_direct_checkout_paths"
    assert MIGRATION_V11.checksum() == EXPECTED_CHECKSUM


def test_v11_allows_two_sessions_to_share_direct_checkout_path(tmp_path: Path) -> None:
    database = tmp_path / "journal.db"
    shared_path = str((tmp_path / "source").resolve())

    with Journal.open(database) as journal:
        journal.create_session(SESSION_1, "repo", BASE_SHA)
        journal.create_session(SESSION_2, "repo", BASE_SHA)

        first = journal.register_workspace(
            session_id=SESSION_1,
            workspace_path=shared_path,
            base_sha=BASE_SHA,
            revision=0,
            state_hash=STATE_HASH,
        )
        second = journal.register_workspace(
            session_id=SESSION_2,
            workspace_path=shared_path,
            base_sha=BASE_SHA,
            revision=0,
            state_hash=STATE_HASH,
        )

        assert first.workspace_path == shared_path
        assert second.workspace_path == shared_path

        journal.record_workspace_preserved(
            session_id=SESSION_1,
            workspace_path=shared_path,
            base_sha=BASE_SHA,
            expected_revision=0,
            expected_state_hash=STATE_HASH,
        )
        journal.record_workspace_preserved(
            session_id=SESSION_2,
            workspace_path=shared_path,
            base_sha=BASE_SHA,
            expected_revision=0,
            expected_state_hash=STATE_HASH,
        )

    connection = sqlite3.connect(database)
    try:
        latest = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert latest == (11, "journal_v11_shared_direct_checkout_paths")
        assert ["workspace_path"] not in unique_index_columns(connection, "workspaces")
        assert ["workspace_path"] not in unique_index_columns(
            connection,
            "workspace_lifecycle",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM workspaces WHERE workspace_path = ?",
            (shared_path,),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_lifecycle WHERE workspace_path = ?",
            (shared_path,),
        ).fetchone()[0] == 2
    finally:
        connection.close()
