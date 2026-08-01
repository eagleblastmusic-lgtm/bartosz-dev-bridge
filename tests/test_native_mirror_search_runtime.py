from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bdb_bridge.native_host import NATIVE_CONFIG_SCHEMA, NATIVE_REQUEST_SCHEMA, NativeArmStore, NativeHostConfig, NativeHostService


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_native_search_text_returns_current_local_match_and_sync_metadata(tmp_path: Path, monkeypatch) -> None:
    fixture = tmp_path / "fixture"
    control = tmp_path / "control"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (fixture, control, worktrees, runtime):
        path.mkdir()
    git(fixture, "init", "-b", "master")
    git(fixture, "config", "user.name", "Native Search")
    git(fixture, "config", "user.email", "native-search@example.invalid")
    (fixture / "assets").mkdir()
    (fixture / "assets" / "theme.css").write_text(".edge { clip-path: inset(0); }\n", encoding="utf-8")
    git(fixture, "add", ".")
    git(fixture, "commit", "-m", "fixture")

    bridge = tmp_path / "bridge.json"
    bridge.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(fixture),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "repository_id": "native-search",
                "allowed_paths": ["assets/**"],
                "workspace_mode": "direct_checkout",
            }
        ),
        encoding="utf-8",
    )
    native = tmp_path / "native.json"
    native.write_text(
        json.dumps(
            {
                "schema": NATIVE_CONFIG_SCHEMA,
                "repositories": {"search": {"bridge_config_path": str(bridge)}},
                "allowed_origins": [ORIGIN],
                "state_path": str(tmp_path / "arm.json"),
                "session_store_path": str(tmp_path / "sessions.json"),
                "max_wait_seconds": 2,
                "max_message_bytes": 65536,
            }
        ),
        encoding="utf-8",
    )

    config = NativeHostConfig.from_json(native)
    NativeArmStore(config.state_path, now_fn=lambda: NOW).arm(minutes=5)
    service = NativeHostService(config, origin=ORIGIN, now_fn=lambda: NOW)

    response = service.handle(
        {
            "schema": NATIVE_REQUEST_SCHEMA,
            "request_id": "search-1",
            "action": "search_text",
            "wait_seconds": 0,
            "bdb_action": {
                "schema": "bdb-action-v1",
                "repo_alias": "search",
                "operation": "search_text",
                "payload": {"query": "clip-path"},
            },
        }
    )
    assert response["status"] == "completed"
    assert response["result"]["operation"] == "search_text"
    assert response["result"]["matches"][0]["path"] == "assets/theme.css"
    assert response["result"]["changed_files"] == []
