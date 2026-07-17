from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bdb_bridge.config import BridgeConfig
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_promoter import WorkspacePromoter, WorkspacePromotionWatcher


SESSION = "795545ec-2d28-46af-a4c4-c40877e9cf2a"
SECOND_SESSION = "3dd5c970-763e-41da-9d40-7a744164b606"


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


def setup(tmp_path: Path, session_id: str = SESSION) -> tuple[BridgeConfig, Path, Path, str]:
    source = tmp_path / "source"
    control = tmp_path / "control"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (source, control, worktrees, runtime):
        path.mkdir()

    git(source, "init")
    git(source, "config", "user.name", "Workspace Promoter Test")
    git(source, "config", "user.email", "workspace-promoter@example.invalid")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    git(source, "add", "--", "app.py")
    git(source, "commit", "-m", "baseline")
    base_sha = git(source, "rev-parse", "HEAD")

    worktree = worktrees / session_id
    git(source, "worktree", "add", "--detach", str(worktree), base_sha)
    (worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")

    config = BridgeConfig(
        control_repo_path=control,
        fixture_repo_path=source,
        worktree_root=worktrees,
        runtime_dir=runtime,
        repository_id="workspace-promoter-test",
        allowed_paths=("app.py",),
    )
    return config, source, worktree, base_sha


def result_path(config: BridgeConfig, session_id: str, *, status: str = "success", exit_code: int = 0) -> Path:
    path = Path(config.direct_result_dir) / "sessions" / session_id / "results" / "000001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": status,
                "exit_code": exit_code,
                "session_id": session_id,
                "sequence": 1,
                "command_id": f"{session_id}:000001",
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


def test_successful_result_creates_commit_and_fast_forwards_source(tmp_path: Path) -> None:
    config, source, worktree, base_sha = setup(tmp_path)
    result = result_path(config, SESSION)

    outcome = WorkspacePromoter(config).promote_file(result)

    assert outcome.status == "promoted"
    assert outcome.source_commit is not None
    assert outcome.source_commit != base_sha
    assert git(source, "rev-parse", "HEAD") == outcome.source_commit
    assert git(source, "status", "--porcelain=v1") == ""
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert git(worktree, "status", "--porcelain=v1") == ""
    assert git(worktree, "rev-parse", "HEAD") == outcome.source_commit

    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "promoted"
    assert receipt["parent_commit"] == base_sha
    assert receipt["source_commit"] == outcome.source_commit
    assert receipt["changed_files"] == ["app.py"]

    repeated = WorkspacePromoter(config).promote_file(result)
    assert repeated.status == "already_promoted"
    assert repeated.source_commit == outcome.source_commit


def test_failed_result_is_not_promoted(tmp_path: Path) -> None:
    config, source, _worktree, base_sha = setup(tmp_path)
    result = result_path(config, SESSION, status="failed", exit_code=1)

    with pytest.raises(BridgeError) as exc:
        WorkspacePromoter(config).promote_file(result)

    assert exc.value.code == "policy_denied"
    assert git(source, "rev-parse", "HEAD") == base_sha
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_watcher_ignores_preexisting_results_and_promotes_only_new_ones(tmp_path: Path) -> None:
    config, source, _first_worktree, base_sha = setup(tmp_path)
    preexisting = result_path(config, SESSION)
    watcher = WorkspacePromotionWatcher(WorkspacePromoter(config))

    assert watcher.initialize_existing() == 1
    assert watcher.scan_once() == []
    assert git(source, "rev-parse", "HEAD") == base_sha

    second_worktree = Path(config.worktree_root) / SECOND_SESSION
    git(source, "worktree", "add", "--detach", str(second_worktree), base_sha)
    (second_worktree / "app.py").write_text("VALUE = 3\n", encoding="utf-8", newline="\n")
    new_result = result_path(config, SECOND_SESSION)

    outcomes = watcher.scan_once()

    assert len(outcomes) == 1
    assert outcomes[0].session_id == SECOND_SESSION
    assert outcomes[0].status == "promoted"
    assert git(source, "rev-parse", "HEAD") == outcomes[0].source_commit
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert preexisting.exists() and new_result.exists()
