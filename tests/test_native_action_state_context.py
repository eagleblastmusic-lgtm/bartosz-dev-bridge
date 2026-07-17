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
from bdb_bridge.workspace_state import clean_workspace_state_hash


NOW = datetime(2026, 7, 17, 3, 0, 0, tzinfo=timezone.utc)
SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"


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


def setup(tmp_path: Path) -> tuple[Path, NativeActionComposer, str]:
    fixture = tmp_path / "fixture"
    control = tmp_path / "control"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (fixture, control, worktrees, runtime):
        path.mkdir()
    git(fixture, "init")
    git(fixture, "config", "user.name", "State Context Test")
    git(fixture, "config", "user.email", "state-context@example.invalid")
    (fixture / "src").mkdir()
    (fixture / "src" / "clamp.py").write_text("value = 1\n", encoding="utf-8")
    git(fixture, "add", "--", "src/clamp.py")
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
                "repository_id": "synthetic",
                "allowed_paths": ["src/clamp.py"],
                "max_sequence": 3,
            }
        ),
        encoding="utf-8",
    )
    repository = RepositoryAlias.load("synthetic", config_path)

    def writer(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    store = NativeSessionStore(tmp_path / "sessions.json", writer=writer)
    composer = NativeActionComposer(
        {"synthetic": repository},
        store,
        now_fn=lambda: NOW,
    )
    return fixture, composer, base_sha


def test_context_exposes_exact_clean_initial_state_hash(tmp_path: Path) -> None:
    _, composer, base_sha = setup(tmp_path)

    context = composer.context("synthetic")

    assert context["base_sha"] == base_sha
    assert context["source_clean"] is True
    assert context["initial_revision"] == 0
    assert context["initial_state_hash"] == clean_workspace_state_hash(base_sha)
    assert context["allowed_paths"] == ["src/clamp.py"]


def test_first_mutating_action_receives_initial_hash_and_trusted_scope(tmp_path: Path) -> None:
    _, composer, base_sha = setup(tmp_path)

    _, envelope = composer.compose(
        {
            "schema": ACTION_SCHEMA,
            "repo_alias": "synthetic",
            "session_id": SESSION,
            "sequence": 1,
            "operation": "multi_file_patch",
            "expected_revision": 0,
            "payload": {"profile_id": "poc_pytest", "patches": []},
        }
    )

    assert envelope["manifest"]["base_sha"] == base_sha
    assert envelope["manifest"]["allowed_paths"] == ["src/clamp.py"]
    assert envelope["command"]["expected_state_hash"] == clean_workspace_state_hash(base_sha)


def test_later_mutating_action_requires_previous_result_hash(tmp_path: Path) -> None:
    _, composer, _ = setup(tmp_path)
    composer.compose(
        {
            "schema": ACTION_SCHEMA,
            "repo_alias": "synthetic",
            "session_id": SESSION,
            "sequence": 1,
            "operation": "open_read",
            "expected_revision": 0,
            "payload": {"path": "src/clamp.py"},
        }
    )

    with pytest.raises(BridgeError) as exc:
        composer.compose(
            {
                "schema": ACTION_SCHEMA,
                "repo_alias": "synthetic",
                "session_id": SESSION,
                "sequence": 2,
                "operation": "multi_file_patch",
                "expected_revision": 0,
                "payload": {"profile_id": "poc_pytest", "patches": []},
            }
        )
    assert exc.value.code == "invalid_payload"


def test_dirty_source_context_is_visible_and_new_session_fails_closed(tmp_path: Path) -> None:
    fixture, composer, _ = setup(tmp_path)
    (fixture / "src" / "clamp.py").write_text("dirty = True\n", encoding="utf-8")

    context = composer.context("synthetic")
    assert context["source_clean"] is False
    assert context["initial_state_hash"] is None

    with pytest.raises(BridgeError) as exc:
        composer.compose(
            {
                "schema": ACTION_SCHEMA,
                "repo_alias": "synthetic",
                "operation": "open_read",
                "payload": {"path": "src/clamp.py"},
            }
        )
    assert exc.value.code == "dirty_source_checkout"
