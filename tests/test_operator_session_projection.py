from __future__ import annotations

from pathlib import Path

from bdb_operator import OperatorApi, SESSION_HISTORY_SCHEMA
from bdb_operator.session_projection import SessionProjectionReader

from .session_projection_fixture import FAILED_SESSION, SUCCESS_SESSION, workspace_fixture


def test_session_projection_shows_failed_and_promoted_sessions_without_linking_them(tmp_path: Path) -> None:
    root, journal, _, _ = workspace_fixture(tmp_path)
    before = journal.read_bytes()

    value = SessionProjectionReader.from_workspace_root(root).list_sessions(limit=10)

    assert value["schema"] == SESSION_HISTORY_SCHEMA
    assert value["read_only"] is True
    assert value["repair_relationships_inferred"] is False
    assert [session["session_id"] for session in value["sessions"]] == [SUCCESS_SESSION, FAILED_SESSION]

    success = value["sessions"][0]
    attempt = success["attempts"][0]
    assert attempt["result"]["checkpoint_state"] == "committed"
    assert attempt["result"]["rollback_performed"] is False
    assert attempt["receipt_file"]["valid"] is True
    assert attempt["receipt"]["source_commit"] == "b" * 40

    failed = value["sessions"][1]
    failed_attempt = failed["attempts"][0]
    assert failed_attempt["result"]["checkpoint_state"] == "rolled_back"
    assert failed_attempt["result"]["rollback_performed"] is True
    assert failed_attempt["receipt"] is None
    assert success["repair_group_id"] is None
    assert failed["repair_group_id"] is None
    assert journal.read_bytes() == before


def test_operator_sessions_does_not_execute_processes(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)

    class NoCommandRunner:
        def run(self, args, *, timeout_seconds):  # pragma: no cover - failure guard
            raise AssertionError(f"Session projection must not execute processes: {args}")

    api = OperatorApi(repo_root=tmp_path, runner=NoCommandRunner(), platform_name="posix")
    response = api.sessions(root, limit=5)

    assert response.ok is True
    assert response.operation == "sessions"
    assert response.data["schema"] == SESSION_HISTORY_SCHEMA
    assert "sessions" in api.capabilities().data["read_operations"]
