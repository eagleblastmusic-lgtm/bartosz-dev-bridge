from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.runtime_hardening import (
    _harden_worktree_add_args,
    _terminal_result_from_journal,
)
from bdb_bridge.workspace_context import WorkspaceContextBuilder


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout.strip()


def test_worktree_add_forces_platform_independent_lf_checkout() -> None:
    hardened = _harden_worktree_add_args(
        ["worktree", "add", "--detach", "C:/tmp/worktree", "a" * 40]
    )
    assert hardened[:4] == [
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.eol=lf",
    ]
    assert hardened[4:] == [
        "worktree",
        "add",
        "--detach",
        "C:/tmp/worktree",
        "a" * 40,
    ]
    assert _harden_worktree_add_args(["status", "--porcelain=v1"]) == [
        "status",
        "--porcelain=v1",
    ]


def test_terminal_state_is_exposed_as_durable_needs_user_result(tmp_path: Path) -> None:
    database = tmp_path / "journal.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE commands (
                command_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL,
                command_commit_sha TEXT,
                expected_revision INTEGER,
                expected_state_hash TEXT,
                command_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE workspaces (
                session_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                state_hash TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO commands (
                command_id, session_id, sequence, state, command_commit_sha,
                expected_revision, expected_state_hash, command_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                1,
                "state_mismatch",
                "b" * 40,
                0,
                "sha256:expected",
                json.dumps(
                    {
                        "operation": "multi_file_patch",
                        "payload": {},
                    }
                ),
                "2026-07-18T15:12:56.000000Z",
                "2026-07-18T15:12:57.000000Z",
            ),
        )
        connection.execute(
            "INSERT INTO workspaces (session_id, revision, state_hash) VALUES (?, ?, ?)",
            (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                0,
                "sha256:physical",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = _terminal_result_from_journal(
        database,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        1,
    )
    assert result is not None
    assert result["status"] == "state_mismatch"
    assert result["error_code"] == "state_mismatch"
    assert result["changed_files"] == []
    assert result["workspace_revision_before"] == 0
    assert result["workspace_revision_after"] == 0
    assert result["state_hash_before"] == "sha256:physical"
    assert result["state_hash_after"] == "sha256:physical"
    assert result["data"] == {
        "operation": "multi_file_patch",
        "terminal": "needs_user",
        "terminal_state": "state_mismatch",
        "rollback_performed": False,
    }


def test_clean_workspace_context_uses_canonical_git_blob_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    source = b"first\nsecond\n"
    (repo / "README.md").write_bytes(source)
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    head = git(repo, "rev-parse", "HEAD")

    runtime = tmp_path / "runtime"
    promotions = runtime / "promotions"
    promotions.mkdir(parents=True)
    physical_hash = "sha256:" + "f" * 64
    (promotions / "receipt.json").write_text(
        json.dumps(
            {
                "schema": "bdb-workspace-promotion-v1",
                "status": "promoted",
                "command_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001",
                "source_commit": head,
                "promoted_at": "2026-07-18T15:52:50.000000Z",
                "changed_files": ["README.md"],
                "file_sha256": {"README.md": physical_hash},
            }
        ),
        encoding="utf-8",
    )

    config = SimpleNamespace(
        fixture_repo_path=str(repo),
        allowed_paths=("README.md",),
        runtime_dir=str(runtime),
    )
    snapshot = WorkspaceContextBuilder(config).build()
    readme = next(
        item for item in snapshot["snapshot_files"] if item["path"] == "README.md"
    )
    canonical_hash = "sha256:" + hashlib.sha256(source).hexdigest()

    assert snapshot["snapshot_source"] == "git_blobs"
    assert snapshot["capabilities"]["canonical_git_blob_hashes"] is True
    assert readme["content"] == source.decode("utf-8")
    assert readme["bytes"] == len(source)
    assert readme["sha256"] == canonical_hash
    assert snapshot["latest_promotion"] == {
        "status": "promoted",
        "command_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:000001",
        "source_commit": head,
        "changed_files": ["README.md"],
        "file_sha256": {"README.md": canonical_hash},
        "promoted_at": "2026-07-18T15:52:50.000000Z",
    }
