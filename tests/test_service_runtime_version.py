from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import BridgeError, Journal
from bdb_bridge.runtime_version import (
    BDB_RUNTIME_VERSION,
    require_compatible_service_runtime,
    service_runtime_status,
)


NOW = "2026-08-02T16:00:00Z"


def test_mutation_runtime_gate_rejects_old_worker_and_accepts_current_worker(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "journal.db"
    journal = Journal.open(journal_path, now_fn=lambda: NOW)
    try:
        old_id = "inst-11111111-1111-1111-1111-111111111111"
        journal.start_service_instance(old_id, 100, NOW)

        old_status = service_runtime_status(journal_path)
        assert old_status["compatible"] is False
        assert old_status["reason"] == "runtime_version_missing"
        with pytest.raises(BridgeError) as error:
            require_compatible_service_runtime(journal_path)
        assert str(error.value.code) == "bridge_restart_required"

        journal.mark_service_instance_stopped(old_id, exit_code=0)
        current_id = "inst-22222222-2222-2222-2222-222222222222"
        journal.start_service_instance(
            current_id,
            200,
            NOW,
            runtime_version=BDB_RUNTIME_VERSION,
        )

        current_status = require_compatible_service_runtime(journal_path)
        assert current_status["compatible"] is True
        assert current_status["runtime_version"] == BDB_RUNTIME_VERSION
        assert current_status["instance_id"] == current_id
    finally:
        journal.close()
