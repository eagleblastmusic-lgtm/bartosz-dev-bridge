from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.repository_inspection import inspect_repository


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_inspect_bundle_combines_search_tree_and_multiple_reads(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Inspection Test")
    git(repo, "config", "user.email", "inspection@example.invalid")
    (repo / "src").mkdir()
    (repo / "private").mkdir()
    (repo / "src" / "alpha.py").write_text(
        "def alpha():\n    return 'needle'\n", encoding="utf-8"
    )
    (repo / "src" / "beta.py").write_text("class Beta:\n    pass\n", encoding="utf-8")
    (repo / "private" / "secret.py").write_text("needle = 'secret'\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    config = SimpleNamespace(
        fixture_repo_path=repo,
        allowed_paths=("src/**",),
        runtime_dir=runtime,
        mirror_sync_enabled=False,
    )

    result = inspect_repository(
        config,
        {
            "searches": [{"query": "needle"}, {"query": "class Beta"}],
            "reads": [{"path": "src/beta.py", "start_line": 1, "end_line": 20}],
            "read_top_matches": 2,
        },
    )

    assert result["status"] == "success"
    assert result["operation"] == "inspect_bundle"
    assert len(result["base_sha"]) == 40
    assert [item["query"] for item in result["searches"]] == ["needle", "class Beta"]
    assert {item["path"] for item in result["tree"]} == {"src/alpha.py", "src/beta.py"}
    assert {item["path"] for item in result["reads"]} == {"src/alpha.py", "src/beta.py"}
    assert all("private/" not in str(item) for item in result["searches"])
    assert result["context"]["capabilities"]["inspect_bundle"] is True


def test_compact_inspect_bundle_is_focused_and_bounded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Inspection Test")
    git(repo, "config", "user.email", "inspection@example.invalid")
    (repo / "src" / "feature").mkdir(parents=True)
    (repo / "src" / "other").mkdir(parents=True)
    (repo / "src" / "feature" / "gui.py").write_text(
        "def quote_screen():\n" + "    preview = 'background'\n" * 300,
        encoding="utf-8",
    )
    (repo / "src" / "other" / "noise.py").write_text(
        "background = 'noise'\n" * 300,
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    config = SimpleNamespace(
        fixture_repo_path=repo,
        allowed_paths=("src/**",),
        runtime_dir=runtime,
        mirror_sync_enabled=False,
    )

    result = inspect_repository(
        config,
        {
            "searches": [
                {
                    "query": "quote_screen",
                    "path_prefixes": ["src/feature"],
                },
                {
                    "query": "background",
                    "path_prefixes": ["src/feature"],
                },
            ],
            "reads": [],
            "read_top_matches": 6,
        },
        compact=True,
    )

    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 20 * 1024
    assert result["response_profile"] == "compact"
    assert result["tree"] == []
    assert result["tree_summary"]["focus_prefixes"] == ["src/feature"]
    assert {read["path"] for read in result["reads"]} == {"src/feature/gui.py"}
    assert all(len(search["matches"]) <= 3 for search in result["searches"])


def test_compact_inspection_merges_explicit_read_with_all_matches_in_same_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Inspection Test")
    git(repo, "config", "user.email", "inspection@example.invalid")
    (repo / "src").mkdir()
    lines = [f"line {index}\n" for index in range(1, 81)]
    lines[19] = 'heading = "shared old text"\n'
    lines[59] = 'runtime_setting = "shared old text"\n'
    (repo / "src" / "page.py").write_text("".join(lines), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    config = SimpleNamespace(
        fixture_repo_path=repo,
        allowed_paths=("src/**",),
        runtime_dir=runtime,
        mirror_sync_enabled=False,
    )

    result = inspect_repository(
        config,
        {
            "searches": [{"query": "shared old text"}],
            "reads": [{"path": "src/page.py", "start_line": 16, "end_line": 24}],
            "read_top_matches": 4,
        },
        compact=True,
    )

    assert len(result["reads"]) == 1
    read = result["reads"][0]
    assert read["source"] == "explicit+search_match"
    assert read["start_line"] <= 20
    assert read["end_line"] >= 60
    assert read["content"].count("shared old text") == 2


def test_compact_inspection_prioritizes_exact_search_cluster_over_broad_early_match(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Inspection Test")
    git(repo, "config", "user.email", "inspection@example.invalid")
    (repo / "templates").mkdir()
    lines = [f'"padding_{index}": "none"\n' for index in range(1, 561)]
    lines[249] = '"intro": "shared phrase"\n'
    lines[474] = '"text": "shared phrase exact suffix"\n'
    lines[527] = '"fm_quote_text": "shared phrase exact suffix"\n'
    (repo / "templates" / "page.json").write_text("".join(lines), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    config = SimpleNamespace(
        fixture_repo_path=repo,
        allowed_paths=("templates/**",),
        runtime_dir=runtime,
        mirror_sync_enabled=False,
    )

    result = inspect_repository(
        config,
        {
            "searches": [
                {"query": "shared phrase exact suffix"},
                {"query": "shared phrase"},
            ],
            "reads": [],
            "read_top_matches": 6,
        },
        compact=True,
    )

    read = result["reads"][0]
    assert read["start_line"] <= 475
    assert read["end_line"] >= 528
    assert read["start_line"] > 250
    assert read["content"].count("shared phrase exact suffix") == 2
