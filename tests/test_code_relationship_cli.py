from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bdb_bridge import InstanceLock
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


def test_cli_index_analyze_search_callers_dependencies(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()

    indexed = run_cli(config, "index", "--ref", commits["commit1"], "--json")
    assert indexed.returncode == 0, indexed.stderr
    assert canonical(indexed.stdout)["commit_sha"] == commits["commit1"]

    analyzed = run_cli(config, "analyze", "--ref", commits["commit1"], "--json")
    assert analyzed.returncode == 0, analyzed.stderr
    analysis_payload = canonical(analyzed.stdout)
    assert analysis_payload["created"] is True
    assert analysis_payload["call_edge_count"] > 0

    replay = run_cli(config, "analyze", "--ref", commits["commit1"], "--json")
    assert replay.returncode == 0, replay.stderr
    assert canonical(replay.stdout)["idempotent"] is True

    search = run_cli(
        config, "search", "--ref", commits["commit1"], "--query", "helper", "--kind", "symbol", "--json"
    )
    assert search.returncode == 0, search.stderr
    results = canonical(search.stdout)["results"]
    helper = next(item for item in results if item["path"] == "pkg/tools.py" and item["qualified_name"] == "helper")

    callers = run_cli(
        config, "callers", "--ref", commits["commit1"], "--symbol-id", helper["symbol_id"], "--json"
    )
    assert callers.returncode == 0, callers.stderr
    caller_payload = canonical(callers.stdout)
    assert {item["expression"] for item in caller_payload["callers"]} >= {"h", "tools.helper"}

    graph = run_cli(
        config,
        "dependencies",
        "--ref",
        commits["commit1"],
        "--path",
        "pkg/cycle_a.py",
        "--direction",
        "outgoing",
        "--depth",
        "5",
        "--edge-kind",
        "call",
        "--max-nodes",
        "20",
        "--json",
    )
    assert graph.returncode == 0, graph.stderr
    graph_payload = canonical(graph.stdout)
    assert graph_payload["cycle"] is True


def test_cli_controlled_failures_have_no_traceback(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()

    missing = run_cli(config, "analyze", "--ref", commits["commit1"], "--json")
    assert missing.returncode != 0
    assert "snapshot_not_found" in missing.stderr
    assert "Traceback" not in missing.stderr

    indexed = run_cli(config, "index", "--ref", commits["commit1"], "--json")
    assert indexed.returncode == 0, indexed.stderr
    unsafe = run_cli(
        config,
        "dependencies",
        "--ref",
        commits["commit1"],
        "--path",
        "../escape.py",
        "--json",
    )
    assert unsafe.returncode != 0
    assert "Traceback" not in unsafe.stderr


def test_cli_analyze_rejects_held_instance_lock(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    config = write_config(tmp_path, cfg)
    journal.close()
    indexed = run_cli(config, "index", "--ref", commits["commit1"], "--json")
    assert indexed.returncode == 0, indexed.stderr

    lock = InstanceLock(Path(cfg.runtime_dir) / "bridge.instance.lock")
    lock.acquire()
    try:
        blocked = run_cli(config, "analyze", "--ref", commits["commit1"], "--json")
    finally:
        lock.release()
    assert blocked.returncode != 0
    assert "instance_already_running" in blocked.stderr
    assert "Traceback" not in blocked.stderr
