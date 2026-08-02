from __future__ import annotations

import json
import os
import subprocess
import sys


def test_lightweight_native_bootstrap_does_not_import_cli_or_analysis_stack() -> None:
    code = """
import json
import os
import sys
os.environ['BDB_LIGHTWEIGHT_NATIVE_HOST'] = '1'
from bdb_bridge.native_runtime_bootstrap import install_native_runtime
install_native_runtime()
print(json.dumps({
    'public_api': 'bdb_bridge._public_api' in sys.modules,
    'cli': 'bdb_bridge.ghb07_cli' in sys.modules,
    'relationships': 'bdb_bridge.code_relationship_service' in sys.modules,
    'native': 'bdb_bridge.native_host' in sys.modules,
}))
"""
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert loaded == {
        "public_api": False,
        "cli": False,
        "relationships": False,
        "native": True,
    }
