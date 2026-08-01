from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.repository_search import search_repository


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def fixture(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Search Test")
    git(repo, "config", "user.email", "search@example.invalid")
    (repo / "assets").mkdir()
    (repo / "snippets").mkdir()
    (repo / "private").mkdir()
    (repo / "assets" / "quote.css").write_text(
        ".quote::after { mask-image: linear-gradient(black, transparent); }\n",
        encoding="utf-8",
    )
    (repo / "snippets" / "quote.liquid").write_text(
        '<div class="quote">Cytat</div>\n',
        encoding="utf-8",
    )
    (repo / "private" / "secret.txt").write_text("mask-image should not leak\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    return SimpleNamespace(
        fixture_repo_path=repo,
        allowed_paths=("assets/**", "snippets/**"),
    )


def test_search_returns_bounded_allowed_matches_with_line_numbers(tmp_path: Path) -> None:
    config = fixture(tmp_path)
    result = search_repository(config, {"query": "mask-image"})
    assert result["status"] == "success"
    assert result["operation"] == "search_text"
    assert result["changed_files"] == []
    assert result["total_matches"] == 1
    assert result["matches"] == [
        {
            "kind": "content",
            "path": "assets/quote.css",
            "line": 1,
            "text": ".quote::after { mask-image: linear-gradient(black, transparent); }",
        }
    ]
    assert all("private/" not in match["path"] for match in result["matches"])


def test_search_supports_prefix_extension_and_case_controls(tmp_path: Path) -> None:
    config = fixture(tmp_path)
    result = search_repository(
        config,
        {
            "query": "cytat",
            "path_prefixes": ["snippets"],
            "extensions": [".liquid"],
            "case_sensitive": False,
            "max_results": 5,
        },
    )
    assert result["total_matches"] == 1
    assert result["matches"][0]["path"] == "snippets/quote.liquid"

    sensitive = search_repository(config, {"query": "cytat", "case_sensitive": True})
    assert sensitive["total_matches"] == 0
