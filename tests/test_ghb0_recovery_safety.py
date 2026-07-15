from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from tests.helpers.recovery_gate_fixture import (
    git,
    remote_bytes,
    remote_commit_count,
    run_cli,
    setup_gate_environment,
    start_service,
    stop_and_wait,
    wait_for,
)


def _publish_once(env) -> None:
    process = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "result_published", (env.command_id,), process=process,
    )
    stop_and_wait(env, process)


def test_command_collision_is_persisted_hash_only_and_restart_stays_blocked(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "command-collision")
    _publish_once(env)
    conn = sqlite3.connect(env.journal)
    original_json, original_hash = conn.execute(
        "SELECT command_json,command_sha256 FROM commands WHERE command_id=?", (env.command_id,)
    ).fetchone()
    conn.close()

    path = env.writer / "sessions" / env.session_id / "commands" / "000001.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["payload"]["new"] = "    return 42\n"
    path.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    git(env.writer, "add", "--", str(path.relative_to(env.writer)))
    git(env.writer, "commit", "-m", "collision")
    git(env.writer, "push", "origin", "commands")

    process = start_service(env)
    wait_for(
        env.journal,
        "SELECT COUNT(*) FROM ingestion_issues WHERE command_id=? AND blocking=1",
        1, (env.command_id,), process=process,
    )
    stop_and_wait(env, process)
    conn = sqlite3.connect(env.journal)
    current_json, current_hash = conn.execute(
        "SELECT command_json,command_sha256 FROM commands WHERE command_id=?", (env.command_id,)
    ).fetchone()
    detail = conn.execute(
        "SELECT detail FROM ingestion_issues WHERE command_id=? ORDER BY issue_id DESC LIMIT 1",
        (env.command_id,),
    ).fetchone()[0]
    conn.close()
    assert (current_json, current_hash) == (original_json, original_hash)
    assert "existing_sha256=" in detail and "incoming_sha256=" in detail
    assert "return 42" not in detail and len(detail) <= 500
    assert (env.worktrees / env.session_id).exists()

    replay = start_service(env)
    time.sleep(0.3)
    stop_and_wait(env, replay)
    conn = sqlite3.connect(env.journal)
    assert conn.execute(
        "SELECT COUNT(*) FROM ingestion_issues WHERE command_id=? AND blocking=1",
        (env.command_id,),
    ).fetchone()[0] == 1
    conn.close()


def test_result_collision_preserves_remote_and_moves_to_manual(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "result-collision")
    fault = run_cli(env, "start", "--foreground", fault="AFTER_STAGE_COMMIT_BEFORE_PUBLISH")
    assert fault.returncode == 2 and "Traceback" not in fault.stderr
    conn = sqlite3.connect(env.journal)
    _result_json, remote_path = conn.execute(
        "SELECT result_json,remote_path FROM results WHERE command_id=?", (env.command_id,)
    ).fetchone()
    conn.close()

    git(env.writer, "checkout", "results")
    collision_path = env.writer / remote_path
    collision_path.parent.mkdir(parents=True, exist_ok=True)
    collision_path.write_bytes(b'{"foreign":true}')
    git(env.writer, "add", "--", remote_path)
    git(env.writer, "commit", "-m", "foreign result")
    git(env.writer, "push", "origin", "results")
    git(env.writer, "checkout", "commands")
    foreign_head_count = remote_commit_count(env)

    recovery = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "manual_reconciliation_required", (env.command_id,), process=recovery,
    )
    stop_and_wait(env, recovery)
    assert remote_bytes(env, remote_path) == b'{"foreign":true}'
    assert remote_commit_count(env) == foreign_head_count
    conn = sqlite3.connect(env.journal)
    command_state = conn.execute(
        "SELECT state FROM commands WHERE command_id=?", (env.command_id,)
    ).fetchone()[0]
    session_state = conn.execute(
        "SELECT state FROM sessions WHERE session_id=?", (env.session_id,)
    ).fetchone()[0]
    outbox_state, diagnostic = conn.execute(
        "SELECT state,last_error FROM outbox WHERE command_id=?", (env.command_id,)
    ).fetchone()
    lifecycle = conn.execute(
        "SELECT disposition,state FROM workspace_lifecycle WHERE session_id=?", (env.session_id,)
    ).fetchone()
    conn.close()
    assert command_state == session_state == "manual_reconciliation_required"
    assert outbox_state == "collision" and len(diagnostic) <= 500
    assert lifecycle[0] == "preserve"
    assert (env.worktrees / env.session_id).exists()

    replay = start_service(env)
    time.sleep(0.3)
    stop_and_wait(env, replay)
    assert remote_commit_count(env) == foreign_head_count


def test_divergent_workspace_is_blocked_and_preserved(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "divergent")
    fault = run_cli(env, "start", "--foreground", fault="AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT")
    assert fault.returncode == 2
    target = env.worktrees / env.session_id / "src" / "clamp.py"
    target.write_text("def clamp_percent(value: int) -> int:\n    return 13\n", encoding="utf-8")

    recovery = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "manual_reconciliation_required", (env.command_id,), process=recovery,
    )
    recovery.communicate(timeout=30)
    conn = sqlite3.connect(env.journal)
    session_state = conn.execute(
        "SELECT state FROM sessions WHERE session_id=?", (env.session_id,)
    ).fetchone()[0]
    lifecycle = conn.execute(
        "SELECT disposition,state FROM workspace_lifecycle WHERE session_id=?", (env.session_id,)
    ).fetchone()
    conn.close()
    assert session_state == "manual_reconciliation_required"
    assert lifecycle[0] == "preserve"
    assert target.read_text(encoding="utf-8").endswith("return 13\n")
    assert (env.worktrees / env.session_id).exists()
    assert git(env.fixture, "status", "--porcelain=v1").stdout.strip() == ""


def test_persisted_transport_retry_restarts_and_publishes(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "retry")
    staged = run_cli(env, "start", "--foreground", fault="AFTER_STAGE_COMMIT_BEFORE_PUBLISH")
    assert staged.returncode == 2
    offline = env.control_remote.with_name("control.offline")
    env.control_remote.rename(offline)
    first = start_service(env)
    wait_for(
        env.journal, "SELECT attempt_count FROM outbox WHERE command_id=?",
        1, (env.command_id,), process=first,
    )
    stop_and_wait(env, first)
    offline.rename(env.control_remote)
    time.sleep(1.2)
    second = start_service(env)
    wait_for(
        env.journal, "SELECT state FROM commands WHERE command_id=?",
        "result_published", (env.command_id,), process=second,
    )
    stop_and_wait(env, second)
    assert remote_commit_count(env) == 1


def test_second_real_process_is_rejected_by_os_lock(tmp_path: Path) -> None:
    env = setup_gate_environment(tmp_path / "second-process")
    first = start_service(env)
    wait_for(
        env.journal,
        "SELECT state FROM service_instances WHERE state='running' ORDER BY rowid DESC LIMIT 1",
        "running", process=first,
    )
    second = run_cli(env, "start", "--foreground")
    assert second.returncode == 1
    assert "already running" in second.stderr.lower()
    assert "Traceback" not in second.stderr
    stop_and_wait(env, first)
