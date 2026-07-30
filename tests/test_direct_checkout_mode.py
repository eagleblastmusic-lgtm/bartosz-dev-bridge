from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bdb_bridge.config import BridgeConfig
from bdb_bridge.journal import Journal
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_manager import WorkspaceManager
from bdb_bridge.workspace_promoter import WorkspacePromoter


SESSION = "795545ec-2d28-46af-a4c4-c40877e9cf2a"


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


def setup_direct(tmp_path: Path) -> tuple[BridgeConfig, Path, str]:
    source = tmp_path / "source"
    control = tmp_path / "control"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (source, control, worktrees, runtime):
        path.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "core.autocrlf", "false")
    git(source, "config", "user.name", "Direct Checkout Test")
    git(source, "config", "user.email", "direct-checkout@example.invalid")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    (source / "notes.txt").write_text("private notes\n", encoding="utf-8", newline="\n")
    git(source, "add", "--", "app.py", "notes.txt")
    git(source, "commit", "-m", "baseline")
    base_sha = git(source, "rev-parse", "HEAD")
    config = BridgeConfig(
        control_repo_path=control,
        fixture_repo_path=source,
        worktree_root=worktrees,
        runtime_dir=runtime,
        journal_path=runtime / "journal.db",
        repository_id="direct-checkout-test",
        allowed_paths=("app.py",),
        workspace_mode="direct_checkout",
    )
    return config, source, base_sha


def result_path(config: BridgeConfig) -> Path:
    path = Path(config.direct_result_dir) / "sessions" / SESSION / "results" / "000001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "success",
                "exit_code": 0,
                "session_id": SESSION,
                "sequence": 1,
                "command_id": f"{SESSION}:000001",
                "changed_files": ["app.py"],
                "data": {
                    "operation": "multi_file_patch",
                    "checkpoint_state": "committed",
                    "rollback_performed": False,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_direct_checkout_registers_source_without_creating_worktree(tmp_path: Path) -> None:
    config, source, base_sha = setup_direct(tmp_path)
    with Journal.open(config.journal_path) as journal:
        journal.create_session(SESSION, config.repository_id, base_sha)
        manager = WorkspaceManager(config, SESSION, base_sha, ["app.py"])
        record = manager.ensure_workspace(journal)

    assert Path(record.workspace_path) == source
    assert manager.path == source
    entries = git(source, "worktree", "list", "--porcelain").splitlines()
    assert sum(line.startswith("worktree ") for line in entries) == 1
    assert git(source, "branch", "--show-current") == "main"
    assert git(source, "status", "--porcelain=v1") == ""


def test_direct_checkout_allows_unrelated_dirty_paths(tmp_path: Path) -> None:
    config, source, base_sha = setup_direct(tmp_path)
    (source / "notes.txt").write_text("local private edit\n", encoding="utf-8", newline="\n")

    with Journal.open(config.journal_path) as journal:
        journal.create_session(SESSION, config.repository_id, base_sha)
        manager = WorkspaceManager(config, SESSION, base_sha, ["app.py"])
        record = manager.ensure_workspace(journal)

    assert Path(record.workspace_path) == source
    assert manager.controlled_changed_paths() == []
    assert manager.unauthorized_changed_paths() == ["notes.txt"]
    assert git(source, "status", "--porcelain=v1") == "M notes.txt"


def test_direct_checkout_rejects_preexisting_controlled_change(tmp_path: Path) -> None:
    config, source, base_sha = setup_direct(tmp_path)
    (source / "app.py").write_text("VALUE = 99\n", encoding="utf-8", newline="\n")

    with Journal.open(config.journal_path) as journal:
        journal.create_session(SESSION, config.repository_id, base_sha)
        manager = WorkspaceManager(config, SESSION, base_sha, ["app.py"])
        with pytest.raises(BridgeError) as exc:
            manager.ensure_workspace(journal)

    assert exc.value.code == "dirty_source_checkout"
    assert "app.py" in str(exc.value)


def test_direct_checkout_promoter_commits_in_place(tmp_path: Path) -> None:
    config, source, base_sha = setup_direct(tmp_path)
    with Journal.open(config.journal_path) as journal:
        journal.create_session(SESSION, config.repository_id, base_sha)

    (source / "app.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
    outcome = WorkspacePromoter(config).promote_file(result_path(config))

    assert outcome.status == "promoted"
    assert outcome.source_commit is not None
    assert git(source, "rev-parse", "HEAD") == outcome.source_commit
    assert git(source, "rev-parse", "HEAD^") == base_sha
    assert git(source, "status", "--porcelain=v1") == ""
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    entries = git(source, "worktree", "list", "--porcelain").splitlines()
    assert sum(line.startswith("worktree ") for line in entries) == 1

    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["workspace_mode"] == "direct_checkout"
    assert receipt["parent_commit"] == base_sha
    assert receipt["preserved_foreign_paths"] == []


def test_direct_checkout_promoter_preserves_unrelated_dirty_paths(tmp_path: Path) -> None:
    config, source, base_sha = setup_direct(tmp_path)
    with Journal.open(config.journal_path) as journal:
        journal.create_session(SESSION, config.repository_id, base_sha)

    (source / "notes.txt").write_text("keep my local edit\n", encoding="utf-8", newline="\n")
    (source / "app.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")

    outcome = WorkspacePromoter(config).promote_file(result_path(config))

    assert outcome.status == "promoted"
    assert git(source, "rev-parse", "HEAD^") == base_sha
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (source / "notes.txt").read_text(encoding="utf-8") == "keep my local edit\n"
    assert git(source, "status", "--porcelain=v1") == "M notes.txt"
    assert git(source, "show", "--name-only", "--format=", "HEAD").splitlines() == ["app.py"]

    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["preserved_foreign_paths"] == ["notes.txt"]


def test_unknown_workspace_mode_is_rejected(tmp_path: Path) -> None:
    control = tmp_path / "control"
    source = tmp_path / "source"
    worktrees = tmp_path / "worktrees"
    for path in (control, source, worktrees):
        path.mkdir()
    with pytest.raises(BridgeError) as exc:
        BridgeConfig(
            control_repo_path=control,
            fixture_repo_path=source,
            worktree_root=worktrees,
            workspace_mode="unsafe",
        )
    assert exc.value.code == "invalid_config"
