from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from tests.helpers.recovery_gate_fixture import run_recovery_gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ghb0-recovery-gate-") as directory:
        report = run_recovery_gate(Path(directory) / "sessions", report_path=args.output)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["failed"] == 0 and report["passed"] >= 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
