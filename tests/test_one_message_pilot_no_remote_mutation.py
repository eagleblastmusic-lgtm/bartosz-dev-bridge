from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_message_pilot_has_no_business_remote_mutation() -> None:
    files = [
        ROOT / "bdb_bridge" / "one_message_pilot.py",
        ROOT / "bdb_bridge" / "one_message_pilot_support.py",
        ROOT / "bdb_bridge" / "one_message_pilot_fixture.py",
        ROOT / "scripts" / "one_message_repair_pilot.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files)

    for forbidden in (
        "git push",
        "merge_pull_request",
        "enable_auto_merge",
        "workflow_dispatch",
        "subprocess.run(command",
        "shell=True",
    ):
        assert forbidden not in combined
