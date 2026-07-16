from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.helpers.code_relationship_fixture import make_relationship_fixture, write_config

ROOT = Path(__file__).resolve().parents[1]


def run_cli(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "bdb_bridge", "bridge", "repo", *args, "--config", str(config)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )


def canonical(stdout: str) -> dict[str, object]:
    payload = json.loads(stdout)
    assert stdout == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    return payload


def test_cli_context_and_gate_json_and_markdown(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()
    assert run_cli(config, "index", "--ref", commits["commit1"], "--json").returncode == 0
    assert run_cli(config, "analyze", "--ref", commits["commit1"], "--json").returncode == 0

    context = run_cli(
        config,
        "context",
        "--ref",
        commits["commit1"],
        "--query",
        "helper",
        "--max-files",
        "5",
        "--max-bytes",
        "4096",
        "--json",
    )
    assert context.returncode == 0, context.stderr
    payload = canonical(context.stdout)
    assert payload["seed_kind"] == "query"
    assert payload["selected_file_count"] <= 5
    assert payload["source_bytes"] <= 4096

    markdown = run_cli(
        config,
        "context",
        "--ref",
        commits["commit1"],
        "--path",
        "pkg/service.py",
        "--depth",
        "0",
        "--max-files",
        "1",
        "--max-bytes",
        "4096",
    )
    assert markdown.returncode == 0, markdown.stderr
    assert markdown.stdout.startswith("# Repository context pack\n")
    assert str(tmp_path) not in markdown.stdout

    gate = run_cli(
        config,
        "gate",
        "--ref",
        commits["commit1"],
        "--sample-max-files",
        "5",
        "--sample-max-bytes",
        "4096",
        "--json",
    )
    assert gate.returncode == 0, gate.stderr
    assert canonical(gate.stdout)["passed"] is True

    failed = run_cli(
        config,
        "gate",
        "--ref",
        commits["commit1"],
        "--max-files",
        "1",
        "--sample-max-files",
        "5",
        "--sample-max-bytes",
        "4096",
        "--json",
    )
    assert failed.returncode == 1
    assert canonical(failed.stdout)["passed"] is False


def test_cli_context_requires_snapshot_and_analysis_without_traceback(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()
    missing_snapshot = run_cli(
        config,
        "context",
        "--ref",
        commits["commit1"],
        "--query",
        "helper",
        "--json",
    )
    assert missing_snapshot.returncode != 0
    assert "snapshot_not_found" in missing_snapshot.stderr
    assert "Traceback" not in missing_snapshot.stderr
    assert run_cli(config, "index", "--ref", commits["commit1"], "--json").returncode == 0
    missing_analysis = run_cli(
        config,
        "gate",
        "--ref",
        commits["commit1"],
        "--json",
    )
    assert missing_analysis.returncode != 0
    assert "analysis_not_found" in missing_analysis.stderr
    assert "Traceback" not in missing_analysis.stderr
