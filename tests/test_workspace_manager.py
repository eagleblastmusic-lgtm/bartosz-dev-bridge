from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
import pytest

from bdb_bridge import Journal, BridgeConfig, BridgeError, BridgeErrorCode, WorkspaceManager

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"

def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed

def init_fixture(tmp_path: Path) -> tuple[Path, str]:
    source = Path(__file__).parents[1] / "bdb-poc-fixture"
    fixture = tmp_path / "fixture"
    shutil.copytree(source, fixture)
    run_git(fixture, "init", "-b", "main")
    run_git(fixture, "config", "user.name", "POC Test")
    run_git(fixture, "config", "user.email", "poc@example.invalid")
    run_git(fixture, "add", "--", ".gitignore", "pyproject.toml", "src", "tests")
    run_git(fixture, "commit", "-m", "fixture baseline")
    return fixture, run_git(fixture, "rev-parse", "HEAD").stdout.strip()

def make_config(tmp_path: Path, fixture: Path) -> BridgeConfig:
    return BridgeConfig(
        control_repo_path=tmp_path / "control",
        fixture_repo_path=fixture,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("src/clamp.py", "tests/test_clamp.py"),
        poll_interval_seconds=0.01,
        max_poll_seconds=30,
        test_timeout_seconds=30,
        python_executable=sys.executable,
    )

def test_workspace_manager_lifecycle(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)

    # 1. ensure_workspace creation flow
    wm = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"])
    rec = wm.ensure_workspace(journal)

    assert rec.session_id == SESSION_ID
    assert rec.base_sha == base_sha
    assert rec.revision == 0
    assert Path(rec.workspace_path) == wm.path

    # Verify worktree HEAD
    head = wm.git.run(["rev-parse", "HEAD"]).stdout.strip()
    assert head == base_sha

    # Verify workspaces table row
    db_rec = journal.get_workspace(SESSION_ID)
    assert db_rec is not None
    assert db_rec.state_hash == rec.state_hash

    # 2. reattach existing registered worktree
    rec2 = wm.ensure_workspace(journal)
    assert rec2.revision == 0
    assert rec2.state_hash == rec.state_hash

    journal.close()

def test_workspace_manager_attach_orphan(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)

    wm = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"])

    # Manually add worktree physical directory without DB registration
    wm.source_git.run(["worktree", "add", "--detach", str(wm.path), base_sha])

    # ensure_workspace should detect and register it
    rec = wm.ensure_workspace(journal)
    assert rec.session_id == SESSION_ID
    assert rec.revision == 0
    assert rec.base_sha == base_sha

    db_rec = journal.get_workspace(SESSION_ID)
    assert db_rec is not None
    assert db_rec.workspace_path == str(wm.path)

    journal.close()

def test_workspace_manager_attach_divergent_orphan_fails(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)

    wm = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"])

    # Create worktree
    wm.source_git.run(["worktree", "add", "--detach", str(wm.path), base_sha])

    # Modify something or add an untracked file to make it dirty
    wm.path.joinpath("src/untracked.py").write_text("print(123)")

    with pytest.raises(BridgeError) as exc:
        wm.ensure_workspace(journal)
    assert exc.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED

    # Verify not registered
    assert journal.get_workspace(SESSION_ID) is None
    journal.close()

def test_workspace_manager_unsafe_path(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)

    # Path escaping root
    wm = WorkspaceManager(config, "../../bad", base_sha, ["src/clamp.py"])
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)

    with pytest.raises(BridgeError) as exc:
        wm.ensure_workspace(journal)
    assert exc.value.code == BridgeErrorCode.UNSAFE_WORKTREE_PATH

    # Policy denied path resolution
    wm_ok = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py"])
    wm_ok.ensure_workspace(journal)

    with pytest.raises(BridgeError) as exc:
        wm_ok.resolve_allowed_path("src/unauthorized.py")
    assert exc.value.code == BridgeErrorCode.POLICY_DENIED

    with pytest.raises(BridgeError) as exc:
        wm_ok.resolve_allowed_path("../../escape.py")
    assert exc.value.code == BridgeErrorCode.UNSAFE_PATH

    journal.close()

def test_workspace_manager_symlink_escape(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)

    wm = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "src/symlink.py"])
    wm.ensure_workspace(journal)

    # Create a symlink pointing outside the workspace inside allowed paths
    target = tmp_path / "outside.txt"
    target.write_text("secrets")

    symlink_path = wm.path / "src" / "symlink.py"
    try:
        os.symlink(target, symlink_path)
    except OSError:
        pytest.skip("Symlink creation is not supported on this user setup")

    with pytest.raises(BridgeError) as exc:
        wm.resolve_allowed_path("src/symlink.py")
    assert exc.value.code == BridgeErrorCode.UNSAFE_PATH

    journal.close()

def test_workspace_manager_read_write_bytes(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)

    wm = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py"])
    wm.ensure_workspace(journal)

    # Read bytes
    content = wm.read_exact_bytes("src/clamp.py")
    assert b"clamp_percent" in content

    # Write planned bytes atomically
    new_content = content + b"\n# a planned line\n"
    wm.write_planned_bytes("src/clamp.py", new_content)

    # Reread exact bytes
    assert wm.read_exact_bytes("src/clamp.py") == new_content
    journal.close()

def test_workspace_manager_state_hash(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)

    wm = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py"])
    wm.ensure_workspace(journal)

    h1 = wm.compute_state_hash()

    # Modify allowed path
    orig = wm.read_exact_bytes("src/clamp.py")
    wm.write_planned_bytes("src/clamp.py", orig + b"\n# change")
    h2 = wm.compute_state_hash()
    assert h1 != h2

    # Check compute_state_hash_with_override matches actual state after modification
    override_hash = wm.compute_state_hash_with_override("src/clamp.py", orig + b"\n# change")
    assert h2 == override_hash

    journal.close()
