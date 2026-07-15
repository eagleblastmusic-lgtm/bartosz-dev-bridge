from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bdb_bridge import BridgeConfig, BridgeError, BridgeErrorCode, Journal, WorkspaceManager

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def init_fixture(tmp_path: Path) -> tuple[Path, str]:
    source = Path(__file__).parents[1] / "bdb-poc-fixture"
    fixture = tmp_path / "fixture"
    shutil.copytree(source, fixture)
    run_git(fixture, "init", "-b", "main")
    run_git(fixture, "config", "user.name", "POC Test")
    run_git(fixture, "config", "user.email", "poc@example.invalid")
    run_git(fixture, "add", "--", ".gitignore", "pyproject.toml", "src", "tests")
    run_git(fixture, "commit", "-m", "fixture baseline")
    return fixture, run_git(fixture, "rev-parse", "HEAD")


def make_config(tmp_path: Path, fixture: Path) -> BridgeConfig:
    return BridgeConfig(
        control_repo_path=tmp_path / "control",
        fixture_repo_path=fixture,
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("src/clamp.py", "tests/test_clamp.py"),
        python_executable=sys.executable,
    )


def test_ghb04_registered_workspace_reattach_validates_exact_identity(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)
    manager = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"])
    first = manager.ensure_workspace(journal)
    second = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"]).ensure_workspace(journal)
    assert second == first
    assert Path(second.workspace_path) == config.worktree_root / SESSION_ID
    assert manager.git.run(["symbolic-ref", "-q", "HEAD"], check=False).returncode != 0
    journal.close()


def test_ghb04_clean_orphan_attach_and_divergent_orphan_rejection(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path / "clean")
    config = make_config(tmp_path / "clean", fixture)
    journal = Journal.open(tmp_path / "clean" / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)
    manager = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"])
    manager.source_git.run(["worktree", "add", "--detach", str(manager.path), base_sha])
    assert manager.ensure_workspace(journal).revision == 0
    journal.close()

    fixture, base_sha = init_fixture(tmp_path / "dirty")
    config = make_config(tmp_path / "dirty", fixture)
    journal = Journal.open(tmp_path / "dirty" / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)
    manager = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"])
    manager.source_git.run(["worktree", "add", "--detach", str(manager.path), base_sha])
    foreign = manager.path / "src" / "foreign.py"
    foreign.write_bytes(b"foreign")
    with pytest.raises(BridgeError) as exc:
        manager.ensure_workspace(journal)
    assert exc.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED
    assert journal.get_workspace(SESSION_ID) is None
    assert foreign.exists()
    journal.close()


def test_ghb04_wrong_head_orphan_is_never_registered(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    (fixture / "second.txt").write_text("second", encoding="utf-8")
    run_git(fixture, "add", "second.txt")
    run_git(fixture, "commit", "-m", "second")
    wrong_head = run_git(fixture, "rev-parse", "HEAD")
    config = make_config(tmp_path, fixture)
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)
    manager = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "tests/test_clamp.py"])
    manager.source_git.run(["worktree", "add", "--detach", str(manager.path), wrong_head])
    with pytest.raises(BridgeError) as exc:
        manager.ensure_workspace(journal)
    assert exc.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED
    assert journal.get_workspace(SESSION_ID) is None
    journal.close()


def test_ghb04_base_sha_and_path_scope_are_strict(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    config = make_config(tmp_path, fixture)
    with pytest.raises(BridgeError) as exc:
        WorkspaceManager(config, SESSION_ID, "main", ["src/clamp.py"])
    assert exc.value.code == BridgeErrorCode.INVALID_BASE_SHA
    with pytest.raises(BridgeError) as exc:
        WorkspaceManager(config, "../../bad", base_sha, ["src/clamp.py"])
    assert exc.value.code == BridgeErrorCode.INVALID_SESSION_ID

    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)
    manager = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py"])
    manager.ensure_workspace(journal)
    with pytest.raises(BridgeError) as exc:
        manager.resolve_allowed_path("tests/test_clamp.py")
    assert exc.value.code == BridgeErrorCode.SCOPE_VIOLATION
    with pytest.raises(BridgeError) as exc:
        manager.resolve_allowed_path("../escape")
    assert exc.value.code == BridgeErrorCode.UNSAFE_PATH
    journal.close()


def test_ghb04_symlink_escape_is_rejected(tmp_path: Path) -> None:
    fixture, base_sha = init_fixture(tmp_path)
    base_config = make_config(tmp_path, fixture)
    config = BridgeConfig(
        control_repo_path=base_config.control_repo_path,
        fixture_repo_path=base_config.fixture_repo_path,
        worktree_root=base_config.worktree_root,
        allowed_paths=("src/clamp.py", "src/link.py"),
        python_executable=sys.executable,
    )
    journal = Journal.open(tmp_path / "journal.db")
    journal.create_session(SESSION_ID, "repo1", base_sha)
    manager = WorkspaceManager(config, SESSION_ID, base_sha, ["src/clamp.py", "src/link.py"])
    manager.ensure_workspace(journal)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = manager.path / "src" / "link.py"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(BridgeError) as exc:
        manager.resolve_allowed_path("src/link.py")
    assert exc.value.code in {BridgeErrorCode.UNSAFE_PATH, BridgeErrorCode.UNSAFE_WORKTREE_PATH}
    journal.close()


def test_ghb04_git_wrapper_maps_os_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from bdb_bridge.workspace_manager import Git

    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(BridgeError) as exc:
        Git(tmp_path).run(["status"])
    assert exc.value.code == BridgeErrorCode.GIT_ERROR
