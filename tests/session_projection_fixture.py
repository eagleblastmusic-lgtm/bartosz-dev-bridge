from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


FAILED_SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
SUCCESS_SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b702"
NOW = "2026-07-19T18:00:00Z"


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def result_document(
    session_id: str,
    status: str,
    exit_code: int,
    checkpoint_state: str,
    rollback: bool,
    changed_files: list[str],
) -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "session_id": session_id,
        "command_id": f"{session_id}:000001",
        "sequence": 1,
        "status": status,
        "error_code": None if status == "success" else "failed",
        "exit_code": exit_code,
        "changed_files": changed_files,
        "data": {
            "operation": "multi_file_patch",
            "checkpoint_state": checkpoint_state,
            "rollback_performed": rollback,
        },
    }


def workspace_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "workspaces" / "sample"
    runtime = tmp_path / "runtime"
    results = runtime / "direct_spool" / "results"
    promotions = runtime / "promotions"
    worktrees = tmp_path / "worktrees"
    for path in (root, runtime, results, promotions, worktrees):
        path.mkdir(parents=True, exist_ok=True)
    journal = runtime / "journal.db"
    config_path = root / "bridge-config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "journal_path": str(journal),
                "runtime_dir": str(runtime),
                "direct_result_dir": str(results),
                "worktree_root": str(worktrees),
            }
        ),
        encoding="utf-8",
    )
    (root / "workspace-loop-state.json").write_text(
        json.dumps(
            {
                "schema": "bdb-workspace-loop-state-v1",
                "alias": "sample",
                "bridge_config": str(config_path),
            }
        ),
        encoding="utf-8",
    )
    create_journal(journal, results, promotions)
    return root, journal, results, promotions


def create_journal(journal: Path, results: Path, promotions: Path) -> None:
    connection = sqlite3.connect(journal)
    connection.executescript(
        """
        CREATE TABLE sessions (
          session_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, base_sha TEXT NOT NULL,
          state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE commands (
          command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
          command_sha256 TEXT NOT NULL, command_json TEXT NOT NULL, command_commit_sha TEXT,
          state TEXT NOT NULL, expected_revision INTEGER, expected_state_hash TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE workspaces (
          session_id TEXT PRIMARY KEY, workspace_path TEXT NOT NULL, base_sha TEXT NOT NULL,
          revision INTEGER NOT NULL, state_hash TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE results (
          command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
          status TEXT NOT NULL, error_code TEXT, result_sha256 TEXT NOT NULL,
          result_json TEXT NOT NULL, remote_path TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE operation_plans (
          command_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, operation TEXT NOT NULL,
          target_path TEXT NOT NULL, profile_id TEXT NOT NULL, expected_revision INTEGER NOT NULL,
          expected_state_hash TEXT, workspace_revision_before INTEGER NOT NULL,
          workspace_state_hash_before TEXT NOT NULL, before_content BLOB NOT NULL,
          before_content_sha256 TEXT NOT NULL, planned_after_content BLOB NOT NULL,
          planned_after_content_sha256 TEXT NOT NULL, planned_after_state_hash TEXT NOT NULL,
          plan_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    failed = result_document(FAILED_SESSION, "failed", 1, "rolled_back", True, [])
    success = result_document(SUCCESS_SESSION, "success", 0, "committed", False, ["src/app.py"])
    insert_session(connection, FAILED_SESSION, "aborted", failed, "2026-07-19T18:01:00Z")
    insert_session(connection, SUCCESS_SESSION, "completed", success, "2026-07-19T18:02:00Z")
    connection.commit()
    connection.close()

    for session_id, document in ((FAILED_SESSION, failed), (SUCCESS_SESSION, success)):
        payload = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
        target = results / "sessions" / session_id / "results" / "000001.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    success_bytes = (json.dumps(success, sort_keys=True) + "\n").encode("utf-8")
    receipt = {
        "schema": "bdb-workspace-promotion-v1",
        "status": "promoted",
        "session_id": SUCCESS_SESSION,
        "sequence": 1,
        "command_id": f"{SUCCESS_SESSION}:000001",
        "result_sha256": sha256(success_bytes),
        "source_commit": "b" * 40,
        "parent_commit": "a" * 40,
        "changed_files": ["src/app.py"],
        "file_sha256": {"src/app.py": "sha256:" + "c" * 64},
        "promoted_at": "2026-07-19T18:03:00Z",
    }
    (promotions / f"{SUCCESS_SESSION}-000001.json").write_text(json.dumps(receipt), encoding="utf-8")


def insert_session(
    connection: sqlite3.Connection,
    session_id: str,
    state: str,
    result: dict[str, object],
    updated: str,
) -> None:
    command_id = f"{session_id}:000001"
    result_bytes = (json.dumps(result, sort_keys=True) + "\n").encode("utf-8")
    connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", (session_id, "repo-sample", "a" * 40, state, NOW, updated))
    connection.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (command_id, session_id, 1, "sha256:command", json.dumps({"operation": "multi_file_patch"}), None,
         "acknowledged" if state == "completed" else "rejected", 0, None, NOW, updated),
    )
    connection.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)", (session_id, f"C:/worktrees/{session_id}", "a" * 40, 1, "sha256:state", NOW, updated))
    connection.execute(
        "INSERT INTO operation_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (command_id, session_id, "multi_file_patch", "src/app.py", "poc_pytest", 0, None, 0,
         "sha256:before", b"before", "sha256:before", b"after", "sha256:after",
         "sha256:planned", "sha256:plan", NOW),
    )
    connection.execute(
        "INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (command_id, session_id, 1, result["status"], result["error_code"], sha256(result_bytes),
         result_bytes.decode("utf-8"), f"sessions/{session_id}/results/000001.json", updated),
    )
