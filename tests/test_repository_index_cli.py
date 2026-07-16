from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bdb_bridge import InstanceLock
from tests.helpers.repository_index_fixture import make_index_fixture, write_config


def _cli(config: Path, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "bdb_bridge", "bridge", *args, "--config", str(config)],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )


def _assert_canonical_json(stdout: str) -> dict:
    assert stdout.endswith("\n")
    body = stdout[:-1]
    parsed = json.loads(body)
    assert body == json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return parsed


def test_repo_status_before_and_after_index(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()
    before = _cli(config, "repo", "status", "--ref", commits["commit1"], "--json")
    assert before.returncode == 0
    assert "Traceback" not in before.stderr
    payload = _assert_canonical_json(before.stdout)
    assert payload["indexed"] is False
    assert payload["snapshot"] is None
    assert payload["commit_sha"] == commits["commit1"]

    indexed = _cli(config, "repo", "index", "--ref", commits["commit1"], "--json")
    assert indexed.returncode == 0
    index_payload = _assert_canonical_json(indexed.stdout)
    assert index_payload["created"] is True
    assert index_payload["commit_sha"] == commits["commit1"]
    assert index_payload["file_count"] > 0

    after = _cli(config, "repo", "status", "--ref", commits["commit1"], "--json")
    assert after.returncode == 0
    after_payload = _assert_canonical_json(after.stdout)
    assert after_payload["indexed"] is True
    assert after_payload["snapshot"]["symbol_count"] == index_payload["symbol_count"]

    again = _cli(config, "repo", "index", "--ref", commits["commit1"], "--json")
    assert again.returncode == 0
    assert _assert_canonical_json(again.stdout)["idempotent"] is True


def test_repo_files_and_outline_json(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()
    assert _cli(config, "repo", "index", "--ref", commits["commit1"], "--json").returncode == 0

    files = _cli(config, "repo", "files", "--ref", commits["commit1"], "--json")
    assert files.returncode == 0
    files_payload = _assert_canonical_json(files.stdout)
    paths = [item["path"] for item in files_payload["files"]]
    assert paths == sorted(paths)
    assert all("content" not in item for item in files_payload["files"])

    outline = _cli(
        config,
        "repo",
        "outline",
        "--ref",
        commits["commit1"],
        "--path",
        "src/sample.py",
        "--json",
    )
    assert outline.returncode == 0
    outline_payload = _assert_canonical_json(outline.stdout)
    assert outline_payload["parse_status"] == "ok"
    assert outline_payload["symbols"]
    assert any(node["qualified_name"] == "Outer" for node in outline_payload["symbols"])

    md = _cli(
        config,
        "repo",
        "outline",
        "--ref",
        commits["commit1"],
        "--path",
        "docs/note.md",
        "--json",
    )
    assert md.returncode == 0
    md_payload = _assert_canonical_json(md.stdout)
    assert md_payload["parse_status"] == "unsupported_language"
    assert md_payload["symbols"] == []


def test_repo_cli_controlled_errors_and_lock(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()

    missing_config = _cli(tmp_path / "missing.json", "repo", "status", "--json")
    assert missing_config.returncode != 0
    assert "Traceback" not in missing_config.stderr

    missing_snapshot = _cli(config, "repo", "files", "--ref", commits["commit1"], "--json")
    assert missing_snapshot.returncode != 0
    assert "Traceback" not in missing_snapshot.stderr
    assert "not_found" in missing_snapshot.stderr

    assert _cli(config, "repo", "index", "--ref", commits["commit1"], "--json").returncode == 0
    missing_path = _cli(
        config,
        "repo",
        "outline",
        "--ref",
        commits["commit1"],
        "--path",
        "no/such/file.py",
        "--json",
    )
    assert missing_path.returncode != 0
    assert "Traceback" not in missing_path.stderr

    unsafe = _cli(
        config,
        "repo",
        "outline",
        "--ref",
        commits["commit1"],
        "--path",
        "../outside.py",
        "--json",
    )
    assert unsafe.returncode != 0
    assert "Traceback" not in unsafe.stderr
    assert "unsafe_path" in unsafe.stderr

    lock = InstanceLock(Path(cfg.runtime_dir) / "bridge.instance.lock")
    lock.acquire()
    try:
        blocked = _cli(config, "repo", "index", "--ref", commits["commit1"], "--json")
        assert blocked.returncode != 0
        assert "Traceback" not in blocked.stderr
        assert "instance_already_running" in blocked.stderr
    finally:
        lock.release()


def test_existing_bridge_commands_still_parse(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()
    result = subprocess.run(
        [sys.executable, "-m", "bdb_bridge", "bridge", "status", "--config", str(config), "--json"],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    assert result.returncode == 0
    assert "Traceback" not in result.stderr
    payload = json.loads(result.stdout)
    assert str(payload["status"]).lower() == "offline"
