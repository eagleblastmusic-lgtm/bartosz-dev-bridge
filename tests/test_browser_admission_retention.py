import json
from pathlib import Path
import subprocess
import sys


def test_real_worker_retains_uncertain_admission_and_replays_evicted_ack():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", str(root / "tests/tools/browser_admission_retention.cjs"), str(root / "browser_extension_vnext/transport_worker.js")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "PASS"
    assert evidence["entries"] <= 128


def test_worker_retention_with_framed_native_and_reopened_canonical_store(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", str(root / "tests/tools/browser_admission_retention.cjs"), str(root / "browser_extension_vnext/transport_worker.js"),
                             sys.executable, str(root / "tests/tools/native_admission_remediation.py"), str(tmp_path)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "PASS" and evidence["framedNative"]
    assert evidence["tasks"] == 551 and evidence["sendCount"] == 551 and evidence["entries"] <= 128
