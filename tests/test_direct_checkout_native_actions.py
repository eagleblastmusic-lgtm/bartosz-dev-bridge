from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.native_actions import (
    ACTION_SCHEMA,
    NativeActionComposer,
    NativeSessionStore,
    RepositoryAlias,
)
from bdb_bridge.native_host import _error_response
from bdb_bridge.workspace_state import clean_workspace_state_hash


NOW = datetime(2026, 7, 31, 0, 0, 0, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def setup_composer(
    tmp_path: Path,
    *,
    workspace_mode: str,
) -> tuple[Path, NativeActionComposer, str]:
    fixture = tmp_path / "fixture"
    control = tmp_path / "control"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (fixture, control, worktrees, runtime):
        path.mkdir()

    git(fixture, "init", "-b", "main")
    git(fixture, "config", "core.autocrlf", "false")
    git(fixture, "config", "user.name", "Native Direct Test")
    git(fixture, "config", "user.email", "native-direct@example.invalid")

    (fixture / "src").mkdir()
    (fixture / "src" / "clamp.py").write_text(
        "value = 1\n",
        encoding="utf-8",
        newline="\n",
    )
    (fixture / "notes.txt").write_text(
        "private notes\n",
        encoding="utf-8",
        newline="\n",
    )
    git(fixture, "add", "--", "src/clamp.py", "notes.txt")
    git(fixture, "commit", "-m", "fixture")
    base_sha = git(fixture, "rev-parse", "HEAD")

    config_path = tmp_path / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(fixture),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "repository_id": "synthetic-direct-native",
                "allowed_paths": ["src/**"],
                "max_sequence": 3,
                "workspace_mode": workspace_mode,
            }
        ),
        encoding="utf-8",
    )
    repository = RepositoryAlias.load("synthetic", config_path)

    def writer(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    composer = NativeActionComposer(
        {"synthetic": repository},
        NativeSessionStore(tmp_path / "sessions.json", writer=writer),
        now_fn=lambda: NOW,
    )
    return fixture, composer, base_sha


def open_read_action() -> dict:
    return {
        "schema": ACTION_SCHEMA,
        "repo_alias": "synthetic",
        "operation": "open_read",
        "payload": {
            "path": "src/clamp.py",
            "start_line": 1,
            "end_line": 20,
        },
    }


def mutating_action() -> dict:
    return {
        "schema": ACTION_SCHEMA,
        "repo_alias": "synthetic",
        "operation": "replace_exact_and_test",
        "payload": {
            "path": "src/clamp.py",
            "old": "value = 1",
            "new": "value = 2",
            "profile_id": "poc_pytest",
        },
    }


def test_direct_checkout_native_session_allows_unrelated_dirty_paths(
    tmp_path: Path,
) -> None:
    fixture, composer, base_sha = setup_composer(
        tmp_path,
        workspace_mode="direct_checkout",
    )
    (fixture / "notes.txt").write_text(
        "keep this local edit\n",
        encoding="utf-8",
        newline="\n",
    )

    context = composer.context("synthetic")
    assert context["source_clean"] is False
    assert context["session_clean"] is True
    assert context["initial_state_hash"] == clean_workspace_state_hash(base_sha)

    _, envelope = composer.compose(open_read_action())
    assert envelope["manifest"]["base_sha"] == base_sha
    assert envelope["manifest"]["allowed_paths"] == ["src/**"]
    assert envelope["command"]["operation"] == "open_read"


def test_direct_checkout_controlled_dirty_path_allows_read_but_rejects_mutation(
    tmp_path: Path,
) -> None:
    fixture, composer, base_sha = setup_composer(
        tmp_path,
        workspace_mode="direct_checkout",
    )
    (fixture / "src" / "clamp.py").write_text(
        "dirty = True\n",
        encoding="utf-8",
        newline="\n",
    )

    context = composer.context("synthetic")
    assert context["source_clean"] is False
    assert context["session_clean"] is False
    assert context["initial_state_hash"] is None

    _, envelope = composer.compose(open_read_action())
    assert envelope["manifest"]["base_sha"] == base_sha
    assert envelope["command"]["expected_state_hash"] is None

    with pytest.raises(BridgeError) as exc:
        composer.compose(mutating_action())
    assert exc.value.code == "dirty_source_checkout"


def test_isolated_worktree_dirty_source_allows_read_but_rejects_mutation(
    tmp_path: Path,
) -> None:
    fixture, composer, base_sha = setup_composer(
        tmp_path,
        workspace_mode="isolated_worktree",
    )
    (fixture / "notes.txt").write_text(
        "unrelated but still blocked for mutations\n",
        encoding="utf-8",
        newline="\n",
    )

    context = composer.context("synthetic")
    assert context["source_clean"] is False
    assert context["session_clean"] is False

    _, envelope = composer.compose(open_read_action())
    assert envelope["manifest"]["base_sha"] == base_sha
    assert envelope["command"]["expected_state_hash"] is None

    with pytest.raises(BridgeError) as exc:
        composer.compose(mutating_action())
    assert exc.value.code == "dirty_source_checkout"


def test_dirty_source_checkout_is_safe_native_client_error() -> None:
    response = _error_response(
        "submit-test",
        BridgeError(
            "dirty_source_checkout",
            "Trusted repository controlled paths must be clean",
        ),
    )

    assert response["status"] == "failed"
    assert response["error"]["code"] == "dirty_source_checkout"
    assert response["error"]["message"] == (
        "Native request failed: dirty_source_checkout"
    )


def test_invalid_payload_native_error_preserves_actionable_validation_message() -> None:
    response = _error_response(
        "inspect-test",
        BridgeError(
            "invalid_payload",
            "inspect_bundle read_top_matches must be boolean or 0-12",
        ),
    )

    assert response["status"] == "failed"
    assert response["error"] == {
        "code": "invalid_payload",
        "message": "inspect_bundle read_top_matches must be boolean or 0-12",
        "details": {
            "rule_id": "invalid_payload",
            "phase": "native_validation",
            "effect_started": False,
        },
    }
