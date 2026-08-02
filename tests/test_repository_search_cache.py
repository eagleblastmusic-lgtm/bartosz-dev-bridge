from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.repository_search import search_repository


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_repository_search_cache_is_scoped_to_git_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    source = repo / "source.py"
    source.write_text("needle = 1\n", encoding="utf-8")
    git(repo, "add", "source.py")
    git(repo, "commit", "-m", "one")
    config = SimpleNamespace(fixture_repo_path=repo, allowed_paths=("*.py",))

    first = search_repository(config, {"query": "needle"})
    second = search_repository(config, {"query": "needle"})
    assert first["cache"]["status"] == "miss"
    assert second["cache"]["status"] == "hit"
    assert second["base_sha"] == first["base_sha"]

    source.write_text("needle = 2\n", encoding="utf-8")
    git(repo, "add", "source.py")
    git(repo, "commit", "-m", "two")
    third = search_repository(config, {"query": "needle"})
    assert third["cache"]["status"] == "miss"
    assert third["base_sha"] != first["base_sha"]
