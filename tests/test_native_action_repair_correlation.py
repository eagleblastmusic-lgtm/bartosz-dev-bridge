from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bdb_bridge.native_actions import (
    NativeActionComposer,
    NativeSessionStore,
    RepositoryAlias,
)
from bdb_bridge.protocol import BridgeError
from bdb_bridge.repair_correlation import REPAIR_CORRELATION_SCHEMA


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
PREDECESSOR = "018f3f66-6cb3-4f66-9f2e-3d7647d1b700"
CORRELATION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b799"
NOW = datetime(2026, 7, 19, 18, 0, 0, tzinfo=timezone.utc)


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


def initialize_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init")
    git(repo, "config", "user.name", "Repair Correlation Test")
    git(repo, "config", "user.email", "repair-correlation@example.invalid")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(repo, "add", "--", "README.md")
    git(repo, "commit", "-m", "initialize fixture")


def writer(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def composer(tmp_path: Path) -> tuple[NativeActionComposer, Path, Path]:
    repo = tmp_path / "repo"
    control = tmp_path / "control"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    initialize_repo(repo)
    for path in (control, worktrees, runtime):
        path.mkdir(parents=True)
    config_path = tmp_path / "bridge-config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(repo),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "repository_id": "sample-repository",
                "allowed_paths": ["README.md"],
                "max_sequence": 3,
            }
        ),
        encoding="utf-8",
    )
    store_path = tmp_path / "native-sessions.json"
    native = NativeActionComposer(
        {"sample": RepositoryAlias.load("sample", config_path)},
        NativeSessionStore(store_path, writer=writer),
        now_fn=lambda: NOW,
    )
    return native, store_path, repo


def action(*, sequence: int, repair_correlation: object = None, include: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "bdb-action-v1",
        "repo_alias": "sample",
        "session_id": SESSION_ID,
        "sequence": sequence,
        "operation": "open_read",
        "expected_revision": 0,
        "payload": {"path": "README.md"},
    }
    if include:
        value["repair_correlation"] = repair_correlation
    return value


def explicit_repair(correlation_id: str = CORRELATION_ID) -> dict[str, object]:
    return {
        "schema": REPAIR_CORRELATION_SCHEMA,
        "correlation_id": correlation_id,
        "role": "repair",
        "predecessor_session_id": PREDECESSOR,
    }


def test_native_action_persists_and_reuses_explicit_repair_correlation(tmp_path: Path) -> None:
    native, store_path, _ = composer(tmp_path)

    _, first = native.compose(action(sequence=1, repair_correlation=explicit_repair()))
    _, second = native.compose(action(sequence=2, include=False))

    assert first["manifest"]["repair_correlation"] == explicit_repair()
    assert second["manifest"]["repair_correlation"] == explicit_repair()
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored["sessions"][SESSION_ID]["repair_correlation"] == explicit_repair()


def test_native_action_rejects_correlation_change_within_session(tmp_path: Path) -> None:
    native, _, _ = composer(tmp_path)
    native.compose(action(sequence=1, repair_correlation=explicit_repair()))

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                sequence=2,
                repair_correlation=explicit_repair(
                    "018f3f66-6cb3-4f66-9f2e-3d7647d1b798"
                ),
            )
        )
    assert error.value.code == "journal_conflict"


def test_uncorrelated_native_session_remains_backward_compatible(tmp_path: Path) -> None:
    native, store_path, _ = composer(tmp_path)

    _, envelope = native.compose(action(sequence=1, include=False))

    assert "repair_correlation" not in envelope["manifest"]
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert "repair_correlation" not in stored["sessions"][SESSION_ID]


def test_dirty_repo_rejects_correlated_session_before_store_write(tmp_path: Path) -> None:
    native, store_path, repo = composer(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    assert git(repo, "status", "--porcelain=v1")

    with pytest.raises(BridgeError) as error:
        native.compose(action(sequence=1, repair_correlation=explicit_repair()))
    assert error.value.code == "dirty_source_checkout"
    assert not store_path.exists()
