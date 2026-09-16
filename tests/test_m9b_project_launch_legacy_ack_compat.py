from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import M9B_NATIVE_REQUEST_SCHEMA, VNextNativeConfig, handle_message


def _config(tmp_path: Path) -> VNextNativeConfig:
    return VNextNativeConfig(
        runtime_root=tmp_path / "vnext",
        legacy_runtime_root=tmp_path / "legacy",
        bootstrap_authority_root=tmp_path / "ProgramData" / "BDB" / "bootstrap",
    )


def test_minimal_transport_ack_does_not_require_conversation_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    sequence: list[str] = []
    launch = SimpleNamespace(launch_id=launch_id)
    launch.to_dict = lambda: {"launch_id": launch_id}

    class _Queue:
        def claim_matches(self, *, launch_id: str, claim_id: str) -> bool:
            return True

        def peek(self):
            return launch

        def acknowledge(self, *, launch_id: str, claim_id: str) -> bool:
            sequence.append("queue_ack")
            return True

    class _Canonical:
        def __init__(self, _runtime_root: Path) -> None:
            pass

        @staticmethod
        def is_canonical_launch(_launch) -> bool:
            return False

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: _Queue())
    monkeypatch.setattr("bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState", _Canonical)

    response = handle_message(
        _config(tmp_path),
        {
            "schema": M9B_NATIVE_REQUEST_SCHEMA,
            "request_id": "legacy-ack-1",
            "action": "project_launch_ack",
            "protocol_generation": PROTOCOL_GENERATION,
            "browser_extension_id": BROWSER_EXTENSION_ID,
            "launch_id": launch_id,
            "claim_id": claim_id,
        },
    )

    assert response["status"] == "acknowledged"
    assert sequence == ["queue_ack"]
