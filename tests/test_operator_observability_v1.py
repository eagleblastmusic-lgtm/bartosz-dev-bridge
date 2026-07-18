from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from bdb_operator import CURRENT_OPERATION_SCHEMA, EVENT_SCHEMA, LOG_SNAPSHOT_SCHEMA, OperatorApi
from bdb_operator.observability import ObservabilityReader


NOW = "2026-07-18T19:30:00Z"


def workspace_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "workspaces" / "calculator2"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    journal = runtime / "journal.db"
    bridge_config = root / "bridge-config.json"
    stdout_log = runtime / "workspace-promoter.stdout.log"
    stderr_log = runtime / "workspace-promoter.stderr.log"

    bridge_config.write_text(
        json.dumps({"schema_version": "1.1", "journal_path": str(journal)}),
        encoding="utf-8",
    )
    state = {
        "schema": "bdb-workspace-loop-state-v1",
        "status": "prepared",
        "alias": "calculator2",
        "source_repo": str(tmp_path / "source"),
        "source_branch": "main",
        "python_executable": str(tmp_path / "python.exe"),
        "native_config": str(tmp_path / "native-host.json"),
        "bridge_config": str(bridge_config),
        "promoter_stdout": str(stdout_log),
        "promoter_stderr": str(stderr_log),
        "allowed_paths": ["README.md", "tests/*.py"],
    }
    (root / "workspace-loop-state.json").write_text(json.dumps(state), encoding="utf-8")
    create_journal(journal)
    return root, journal, stdout_log, stderr_log


def create_journal(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
          session_id TEXT PRIMARY KEY,
          repository_id TEXT NOT NULL,
          base_sha TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE commands (
          command_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          command_sha256 TEXT NOT NULL,
          command_json TEXT NOT NULL,
          command_commit_sha TEXT,
          state TEXT NOT NULL,
          expected_revision INTEGER,
          expected_state_hash TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE workspaces (
          session_id TEXT PRIMARY KEY,
          workspace_path TEXT NOT NULL,
          base_sha TEXT NOT NULL,
          revision INTEGER NOT NULL,
          state_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE results (
          command_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          status TEXT NOT NULL,
          error_code TEXT,
          result_sha256 TEXT NOT NULL,
          result_json TEXT NOT NULL,
          remote_path TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT,
          command_id TEXT,
          event_type TEXT NOT NULL,
          payload_json TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE operation_plans (
          command_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          operation TEXT NOT NULL,
          target_path TEXT NOT NULL,
          profile_id TEXT NOT NULL,
          expected_revision INTEGER NOT NULL,
          expected_state_hash TEXT,
          workspace_revision_before INTEGER NOT NULL,
          workspace_state_hash_before TEXT NOT NULL,
          before_content BLOB NOT NULL,
          before_content_sha256 TEXT NOT NULL,
          planned_after_content BLOB NOT NULL,
          planned_after_content_sha256 TEXT NOT NULL,
          planned_after_state_hash TEXT NOT NULL,
          plan_sha256 TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    session = "session-00000000-0000-4000-8000-000000000001"
    command = f"{session}:000001"
    command_json = json.dumps(
        {
            "operation": "multi_file_patch",
            "payload": {
                "profile_id": "poc_pytest",
                "secret_content": "must-not-appear-in-current-operation",
            },
        }
    )
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (session, "bdb-workspace-calculator2", "a" * 40, "active", NOW, NOW),
    )
    connection.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (command, session, 1, "sha256:command", command_json, None, "executing", 0, None, NOW, NOW),
    )
    connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session, "C:/worktree", "a" * 40, 0, "sha256:state", NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO operation_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            command,
            session,
            "multi_file_patch",
            "README.md",
            "poc_pytest",
            0,
            None,
            0,
            "sha256:state",
            b"before",
            "sha256:before",
            b"after",
            "sha256:after",
            "sha256:planned-state",
            "sha256:plan",
            NOW,
        ),
    )
    events = [
        (session, None, "session.created", '{"state":"created"}', "2026-07-18T19:29:58Z"),
        (session, command, "command.state_changed", '{"to_state":"executing"}', "2026-07-18T19:29:59Z"),
        (session, command, "result.failed", '{"status":"failed"}', NOW),
        (session, command, "diagnostic.payload", "not-json", "2026-07-18T19:30:01Z"),
    ]
    connection.executemany(
        "INSERT INTO events (session_id, command_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        events,
    )
    connection.commit()
    connection.close()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_events_are_stable_paginated_and_bounded(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    reader = ObservabilityReader.from_workspace_root(root)

    first = reader.list_events(limit=2)
    second = reader.list_events(after_event_id=first["cursor"]["next_after_event_id"], limit=2)

    assert [event["event_id"] for event in first["events"]] == [
        "journal:calculator2:1",
        "journal:calculator2:2",
    ]
    assert first["cursor"] == {"after_event_id": 0, "next_after_event_id": 2, "has_more": True}
    assert [event["sequence"] for event in second["events"]] == [3, 4]
    assert second["events"][0]["severity"] == "error"
    assert second["events"][1]["severity"] == "warning"
    assert second["events"][1]["payload"]["invalid_json"] is True
    assert all(event["schema"] == EVENT_SCHEMA for event in first["events"] + second["events"])


def test_event_filters_do_not_change_cursor_identity(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    reader = ObservabilityReader.from_workspace_root(root)
    command_id = "session-00000000-0000-4000-8000-000000000001:000001"

    result = reader.list_events(command_id=command_id, limit=10)

    assert [event["sequence"] for event in result["events"]] == [2, 3, 4]
    assert all(event["command_id"] == command_id for event in result["events"])
    assert result["filters"]["command_id"] == command_id


def test_journal_file_is_not_modified_by_projections(tmp_path: Path) -> None:
    root, journal, _, _ = workspace_fixture(tmp_path)
    reader = ObservabilityReader.from_workspace_root(root)
    before_hash = sha256_file(journal)
    before_mtime = journal.stat().st_mtime_ns

    reader.list_events(limit=10)
    reader.current_operation()

    assert sha256_file(journal) == before_hash
    assert journal.stat().st_mtime_ns == before_mtime


def test_current_operation_returns_summary_without_command_payload(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    reader = ObservabilityReader.from_workspace_root(root)

    result = reader.current_operation()

    assert result["schema"] == CURRENT_OPERATION_SCHEMA
    assert result["active"] is True
    operation = result["operation"]
    assert operation["state"] == "executing"
    assert operation["operation"] == "multi_file_patch"
    assert operation["target_path"] == "README.md"
    assert operation["profile_id"] == "poc_pytest"
    serialized = json.dumps(result)
    assert "secret_content" not in serialized
    assert "must-not-appear" not in serialized
    assert "command_json" not in serialized


def test_current_operation_is_empty_after_terminal_transition(tmp_path: Path) -> None:
    root, journal, _, _ = workspace_fixture(tmp_path)
    connection = sqlite3.connect(journal)
    connection.execute("UPDATE commands SET state = 'acknowledged'")
    connection.commit()
    connection.close()

    result = ObservabilityReader.from_workspace_root(root).current_operation()

    assert result["active"] is False
    assert result["operation"] is None


def test_log_snapshot_is_bounded_and_declared_paths_only(tmp_path: Path) -> None:
    root, _, stdout_log, stderr_log = workspace_fixture(tmp_path)
    stdout_log.write_text("\n".join(f"line-{index:03d}" for index in range(100)) + "\n", encoding="utf-8")
    stderr_log.write_text("warning-one\nwarning-two\n", encoding="utf-8")

    result = ObservabilityReader.from_workspace_root(root).log_snapshot(max_bytes=256, max_lines=5)

    assert result["schema"] == LOG_SNAPSHOT_SCHEMA
    assert result["limits"] == {"max_bytes_per_source": 256, "max_lines_per_source": 5}
    stdout = result["sources"][0]
    stderr = result["sources"][1]
    assert stdout["source"] == "promoter_stdout"
    assert stdout["lines"] == ["line-095", "line-096", "line-097", "line-098", "line-099"]
    assert stdout["truncated"] is True
    assert stderr["lines"] == ["warning-one", "warning-two"]
    assert stderr["truncated"] is False


def test_public_operator_api_exposes_read_only_observability_without_runner(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)

    class NoCommandRunner:
        def run(self, args, *, timeout_seconds):  # pragma: no cover - failure guard
            raise AssertionError(f"Observability must not execute processes: {args}")

    api = OperatorApi(repo_root=tmp_path, runner=NoCommandRunner(), platform_name="posix")

    capabilities = api.capabilities()
    events = api.events(root, limit=1)
    current = api.current_operation(root)
    logs = api.logs(root, max_bytes=128, max_lines=10)

    assert capabilities.ok is True
    assert capabilities.data["journal_access"] == "read_only"
    assert {"events", "current_operation", "logs"}.issubset(capabilities.data["read_operations"])
    assert events.ok is True and events.project_alias == "calculator2"
    assert current.ok is True and current.data["schema"] == CURRENT_OPERATION_SCHEMA
    assert logs.ok is True and logs.data["schema"] == LOG_SNAPSHOT_SCHEMA


def test_missing_journal_uses_stable_error(tmp_path: Path) -> None:
    root, journal, _, _ = workspace_fixture(tmp_path)
    journal.unlink()
    api = OperatorApi(repo_root=tmp_path, platform_name="posix")

    response = api.events(root)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "journal_missing"


def test_invalid_limits_are_rejected_before_io(tmp_path: Path) -> None:
    root, journal, _, _ = workspace_fixture(tmp_path)
    before_hash = sha256_file(journal)
    api = OperatorApi(repo_root=tmp_path, platform_name="posix")

    events = api.events(root, limit=501)
    logs = api.logs(root, max_bytes=65_537)

    assert events.ok is False and events.error is not None
    assert events.error.code == "invalid_argument"
    assert logs.ok is False and logs.error is not None
    assert logs.error.code == "invalid_argument"
    assert sha256_file(journal) == before_hash
