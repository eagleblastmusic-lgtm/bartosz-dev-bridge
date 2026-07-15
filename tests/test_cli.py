from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path
import pytest


def test_cli_help() -> None:
    # Run bdb --help
    res = subprocess.run([sys.executable, "-m", "bdb_bridge", "--help"], capture_output=True, text=True, check=True)
    assert "usage: bdb" in res.stdout
    assert "{bridge}" in res.stdout


def test_cli_missing_config(tmp_path: Path) -> None:
    # Run bdb bridge start without an actual config file existing
    non_existent = tmp_path / "missing.json"
    res = subprocess.run(
        [sys.executable, "-m", "bdb_bridge", "bridge", "start", "--config", str(non_existent)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "Config file not found" in res.stderr


def test_cli_invalid_config(tmp_path: Path) -> None:
    # Write invalid JSON config
    bad_config = tmp_path / "bad.json"
    bad_config.write_text("invalid json {", encoding="utf-8")
    
    res = subprocess.run(
        [sys.executable, "-m", "bdb_bridge", "bridge", "start", "--config", str(bad_config)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "Failed to load config" in res.stderr
