from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "spikes" / "gui" / "pyside6_probe.py"


def test_pyside6_probe_headless_smoke(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    report_path = tmp_path / "pyside6-probe.json"
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"

    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--headless-smoke",
            "--json-out",
            str(report_path),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == "bdb-gui-technology-spike-v1"
    assert report["candidate"] == "pyside6-qt-widgets"
    assert report["status"] == "success"
    assert report["headless_smoke"] is True
    assert report["window_constructed"] is True
    assert report["event_loop_exit_code"] == 0
    assert report["operator_response_schema"] == "bdb-operator-response-v1"
    assert report["operator_network_listener"] is False
    assert report["operator_arbitrary_shell"] is False
    assert report["mutation_operations_invoked"] == 0
    assert report["qt_platform"] == "offscreen"
    assert report["primary_screen_available"] is True
    assert report["device_pixel_ratio"] is not None
