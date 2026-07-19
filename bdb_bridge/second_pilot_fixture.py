from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .one_message_pilot_support import ORIGIN, content_fields, git


ALIAS = "inventory2"
PROFILE_ID = "poc_unittest"
EXPECTED_FAILED_TEST = "test_rejects_negative_quantity"
ALLOWED_PATHS = [
    "inventory/parser.py",
    "inventory/report.py",
    "tests/test_inventory_report.py",
    "SECOND_PILOT_RESULT.md",
]


def initialize_inventory2(root: Path) -> dict[str, Any]:
    fixture = root / "inventory2"
    fixture.mkdir()
    git(fixture, "init")
    git(fixture, "config", "core.autocrlf", "false")
    git(fixture, "config", "user.name", "BDB Second Pilot")
    git(fixture, "config", "user.email", "second-pilot@example.invalid")
    (fixture / "inventory").mkdir()
    (fixture / "tests").mkdir()
    (fixture / "inventory" / "__init__.py").write_text("", encoding="utf-8")
    (fixture / "tests" / "test_health.py").write_text(
        "import unittest\n\n"
        "class HealthTest(unittest.TestCase):\n"
        "    def test_fixture_is_ready(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    (fixture / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )
    git(fixture, "add", "--", ".")
    git(fixture, "commit", "-m", "initialize inventory2 pilot")

    parser_failed = (
        b"def parse_stock_line(line: str) -> tuple[str, int]:\n"
        b"    sku_text, quantity_text = line.split(',', 1)\n"
        b"    sku = sku_text.strip()\n"
        b"    if not sku:\n"
        b"        raise ValueError('SKU is required')\n"
        b"    quantity = int(quantity_text.strip())\n"
        b"    return sku, quantity\n"
    )
    parser_repaired = (
        b"def parse_stock_line(line: str) -> tuple[str, int]:\n"
        b"    sku_text, quantity_text = line.split(',', 1)\n"
        b"    sku = sku_text.strip()\n"
        b"    if not sku:\n"
        b"        raise ValueError('SKU is required')\n"
        b"    quantity = int(quantity_text.strip())\n"
        b"    if quantity < 0:\n"
        b"        raise ValueError('Quantity cannot be negative')\n"
        b"    return sku, quantity\n"
    )
    report_module = (
        b"from collections.abc import Iterable\n\n"
        b"from .parser import parse_stock_line\n\n"
        b"def summarize_stock(lines: Iterable[str]) -> dict[str, int]:\n"
        b"    totals: dict[str, int] = {}\n"
        b"    for line in lines:\n"
        b"        sku, quantity = parse_stock_line(line)\n"
        b"        totals[sku] = totals.get(sku, 0) + quantity\n"
        b"    return totals\n"
    )
    tests = (
        b"import unittest\n\n"
        b"from inventory.report import summarize_stock\n\n"
        b"class InventoryReportTest(unittest.TestCase):\n"
        b"    def test_sums_duplicate_skus(self):\n"
        b"        self.assertEqual(summarize_stock(['A-1,2', 'A-1,3']), {'A-1': 5})\n\n"
        b"    def test_trims_sku_and_quantity(self):\n"
        b"        self.assertEqual(summarize_stock(['  B-2 , 4 ']), {'B-2': 4})\n\n"
        b"    def test_rejects_negative_quantity(self):\n"
        b"        with self.assertRaises(ValueError):\n"
        b"            summarize_stock(['C-3,-1'])\n"
    )
    return {
        "fixture": fixture,
        "base_sha": git(fixture, "rev-parse", "HEAD"),
        "parser_failed": parser_failed,
        "parser_repaired": parser_repaired,
        "report_module": report_module,
        "tests": tests,
    }


def build_configs(
    root: Path,
    fixture: Path,
    control: Path,
    python_executable: str,
) -> tuple[Path, Path]:
    runtime = root / "runtime"
    runtime.mkdir()
    bridge_config_path = root / "bridge-config.json"
    bridge_config = {
        "schema_version": "1.1",
        "control_repo_path": str(control),
        "fixture_repo_path": str(fixture),
        "worktree_root": str(root / "worktrees"),
        "runtime_dir": str(runtime),
        "journal_path": str(runtime / "journal.db"),
        "repository_id": "bdb-second-inventory2-pilot",
        "allowed_paths": ALLOWED_PATHS,
        "commands_ref": "origin/commands",
        "results_ref": "origin/results",
        "python_executable": python_executable,
        "test_timeout_seconds": 60,
        "heartbeat_interval_seconds": 0.2,
        "heartbeat_stale_seconds": 5,
        "idle_poll_seconds": 0.2,
        "direct_spool_enabled": True,
    }
    bridge_config_path.write_text(
        json.dumps(bridge_config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    native_config_path = root / "native-host.json"
    native_config = {
        "schema": "bdb-native-host-config-v1",
        "repositories": {ALIAS: {"bridge_config_path": str(bridge_config_path)}},
        "allowed_origins": [ORIGIN],
        "state_path": str(root / "native-host-arm.json"),
        "session_store_path": str(root / "native-host-sessions.json"),
        "max_wait_seconds": 120,
        "max_message_bytes": 1048576,
    }
    native_config_path.write_text(
        json.dumps(native_config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bridge_config_path, native_config_path


def create_file(path: str, content: bytes) -> dict[str, Any]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "create_file",
        "path": path,
        **content_fields(content),
    }
