from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge.mirror_sync import MirrorSynchronizer
from bdb_bridge.protocol import BridgeError


def git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check:
        assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def setup(tmp_path: Path) -> tuple[Path, Path, SimpleNamespace, str]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(source, "init", "-b", "master")
    git(source, "config", "user.name", "Mirror Test")
    git(source, "config", "user.email", "mirror@example.invalid")
    (source / "assets").mkdir()
    (source / "assets" / "theme.css").write_text("body { color: black; }\n", encoding="utf-8")
    (source / "notes.txt").write_text("outside\n", encoding="utf-8")
    git(source, "add", "--", "assets/theme.css", "notes.txt")
    git(source, "commit", "-m", "initial")
    head = git(source, "rev-parse", "HEAD")
    git(source, "remote", "add", "bdbmirror", remote.as_uri())
    config = SimpleNamespace(
        fixture_repo_path=source,
        runtime_dir=runtime,
        allowed_paths=("assets/**",),
        mirror_sync_enabled=True,
        mirror_remote_name="bdbmirror",
        mirror_remote_url=remote.as_uri(),
        mirror_local_branch="master",
        mirror_remote_branch="main",
        mirror_timeout_seconds=30.0,
    )
    # Test-only local file remotes are accepted by replacing validated settings
    # after construction. Production BridgeConfig requires credential-free HTTPS.
    sync = object.__new__(MirrorSynchronizer)
    sync.config = config
    sync.source = source.resolve()
    from bdb_bridge.workspace_manager import Git
    from bdb_bridge.mirror_sync import MirrorSyncSettings
    sync.git = Git(sync.source)
    sync.status_path = runtime / "mirror-sync-status.json"
    sync.settings = MirrorSyncSettings(
        True,
        "bdbmirror",
        remote.as_uri(),
        "master",
        "main",
        30.0,
    )
    config._test_sync = sync
    return source, remote, config, head


def test_initial_push_then_up_to_date(tmp_path: Path) -> None:
    source, remote, config, head = setup(tmp_path)
    sync: MirrorSynchronizer = config._test_sync

    first = sync.sync(phase="pre_action")
    assert first is not None
    assert first["status"] == "synced"
    assert first["pushed"] is True
    assert git(remote, "rev-parse", "refs/heads/main") == head

    second = sync.sync(phase="pre_action")
    assert second is not None
    assert second["status"] == "up_to_date"
    assert second["pushed"] is False
    cached = json.loads((Path(config.runtime_dir) / "mirror-sync-status.json").read_text(encoding="utf-8"))
    assert cached["local_head"] == head


def test_read_sync_reuses_recent_verified_head_but_pre_action_rechecks_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, _remote, config, _head = setup(tmp_path)
    sync: MirrorSynchronizer = config._test_sync
    first = sync.sync(phase="pre_inspect_bundle")
    assert first is not None

    calls = 0
    original = sync._remote_head

    def counted_remote_head() -> str | None:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(sync, "_remote_head", counted_remote_head)
    cached = sync.sync(phase="pre_search_text")
    assert cached is not None
    assert cached["cached"] is True
    assert calls == 0

    action = sync.sync(phase="pre_action")
    assert action is not None
    assert action["cached"] is False
    assert calls == 1


def test_unrelated_dirty_path_is_preserved_but_controlled_dirty_is_blocked(tmp_path: Path) -> None:
    source, _remote, config, _head = setup(tmp_path)
    sync: MirrorSynchronizer = config._test_sync
    (source / "notes.txt").write_text("unrelated local change\n", encoding="utf-8")
    assert sync.sync(phase="pre_action")["status"] == "synced"

    (source / "assets" / "theme.css").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        sync.sync(phase="pre_action")
    assert str(exc.value.code) == "dirty_source_checkout"
    status = sync.read_status()
    assert status is not None
    assert status["status"] == "failed"


def test_expected_post_promotion_head_is_enforced(tmp_path: Path) -> None:
    _source, _remote, config, _head = setup(tmp_path)
    sync: MirrorSynchronizer = config._test_sync
    with pytest.raises(BridgeError) as exc:
        sync.sync(phase="post_promotion", expected_head="0" * 40)
    assert str(exc.value.code) == "mirror_sync_failed"
