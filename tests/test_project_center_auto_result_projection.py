from __future__ import annotations

import json
import sqlite3

from bdb_vnext.project_center_auto import CanonicalProjectCenterAutoCommands, ProjectCenterAutoViewModel


def _write_cursor(tmp_path, *, status: str = "ACTIVE", disposition: str = "INITIALIZED", reason_code: str = "AUTO_STARTED") -> None:
    db_path = tmp_path / "control" / "project-memory-v2" / "project-1.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE projects (project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
        conn.execute(
            """
            CREATE TABLE scope_cursors (
                project_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                scope_epoch INTEGER NOT NULL,
                run_id TEXT,
                current_milestone_id TEXT,
                current_task_id TEXT,
                plan_version INTEGER,
                state_revision INTEGER,
                disposition TEXT,
                status TEXT,
                explanation_json TEXT
            )
            """
        )
        conn.execute("INSERT INTO projects (project_id, revision) VALUES (?, ?)", ("project-1", 7))
        conn.execute(
            """
            INSERT INTO scope_cursors (
                project_id, scope, scope_epoch, run_id, current_milestone_id,
                current_task_id, plan_version, state_revision, disposition,
                status, explanation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "project-1",
                "MILESTONE",
                1,
                "run-1",
                "P1",
                "P1-01",
                1,
                7,
                disposition,
                status,
                json.dumps({"reason_code": reason_code, "explanation": f"cursor:{reason_code}"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_read_only_snapshot_projects_project_execution_failure_from_v1(tmp_path) -> None:
    _write_cursor(tmp_path)
    memory = {
        "revision": 9,
        "execution": {
            "task_statuses": {"P1-01": "blocked"},
            "attempts": [
                {
                    "task_id": "P1-01",
                    "result_status": "FAIL",
                    "failure_code": "validation_failed",
                    "result_summary": "Validation failed after Submit result.",
                }
            ],
        },
    }
    commands = CanonicalProjectCenterAutoCommands(tmp_path, "project-1", memory_provider=lambda: memory)

    state = commands.snapshot(plan_available=True, plan_version="1")
    view = ProjectCenterAutoViewModel.from_canonical(state)

    assert state.scope_status == "BLOCKED"
    assert state.reason_code == "validation_failed"
    assert state.reason == "Validation failed after Submit result."
    assert view.can_continue is False
    assert view.can_stop is False


def test_nonblocked_v1_task_keeps_active_cursor_projection(tmp_path) -> None:
    _write_cursor(tmp_path)
    memory = {"revision": 9, "execution": {"task_statuses": {"P1-01": "active"}, "attempts": []}}
    commands = CanonicalProjectCenterAutoCommands(tmp_path, "project-1", memory_provider=lambda: memory)

    state = commands.snapshot(plan_available=True, plan_version="1")
    view = ProjectCenterAutoViewModel.from_canonical(state)

    assert state.scope_status == "ACTIVE"
    assert state.reason_code == "AUTO_STARTED"
    assert view.can_continue is True


def test_stop_fence_keeps_precedence_over_v1_task_blocker(tmp_path) -> None:
    _write_cursor(tmp_path, status="STOPPED", disposition="STOPPED", reason_code="STOPPED")
    memory = {
        "revision": 9,
        "execution": {
            "task_statuses": {"P1-01": "blocked"},
            "attempts": [
                {
                    "task_id": "P1-01",
                    "result_status": "FAIL",
                    "failure_code": "validation_failed",
                    "result_summary": "Validation failed after Submit result.",
                }
            ],
        },
    }
    commands = CanonicalProjectCenterAutoCommands(tmp_path, "project-1", memory_provider=lambda: memory)

    state = commands.snapshot(plan_available=True, plan_version="1")

    assert state.scope_status == "STOPPED"
    assert state.reason_code == "STOPPED"
