from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, OutboxProcessState, RemoteResult, RemoteResultState
from bdb_bridge.local_mirroring_outbox import LocalMirroringOutboxProcessor
from bdb_bridge.local_result_sink import LocalResultSink
from bdb_bridge.local_wake import BridgeWakeWaiter, signal_running_bridge, wake_event_name
from tests.helpers.result_outbox_fixture import COMMAND_ID, NOW, make_journal, stage


@dataclass
class OfflineTransport:
    def fetch_results_head(self) -> str:
        raise BridgeError("transport_unavailable", "offline")

    def read_result(self, remote_path: str) -> RemoteResult:
        return RemoteResult(
            RemoteResultState.UNAVAILABLE,
            remote_path,
            None,
            None,
            None,
            None,
            "offline",
        )

    def publish_result(self, **kwargs):
        raise AssertionError("offline transport must not publish")


def test_local_result_sink_is_exact_idempotent_and_collision_safe(tmp_path: Path) -> None:
    sink = LocalResultSink(tmp_path / "results")
    remote_path = "sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/results/000001.json"

    first = sink.publish(remote_path, b'{"status":"success"}')
    replay = sink.publish(remote_path, b'{"status":"success"}')

    assert replay == first
    assert sink.read(remote_path) == b'{"status":"success"}'
    with pytest.raises(BridgeError) as exc:
        sink.publish(remote_path, b'{"status":"failed"}')
    assert exc.value.code == "journal_conflict"


def test_local_result_is_durable_before_unavailable_git_retry(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    staged, _, outbox = stage(journal)
    sink = LocalResultSink(tmp_path / "local-results")
    processor = LocalMirroringOutboxProcessor(
        journal,
        OfflineTransport(),
        now_fn=lambda: NOW,
        result_sink=sink,
    )

    outcome = processor.process_command(COMMAND_ID)

    assert outcome.state == OutboxProcessState.RETRY_SCHEDULED
    assert sink.read(outbox.remote_path) == staged.result_bytes
    assert journal.get_outbox(COMMAND_ID).attempt_count == 1
    journal.close()


def test_bridge_wake_waiter_round_trip(tmp_path: Path) -> None:
    waiter = BridgeWakeWaiter(tmp_path / "runtime")
    try:
        waiter.set()
        assert waiter.wait(0.1)
        waiter.clear()
        assert not waiter.wait(0.01)
    finally:
        waiter.close()


def test_wake_event_name_is_stable_and_runtime_scoped(tmp_path: Path) -> None:
    first = wake_event_name(tmp_path / "runtime-a")
    replay = wake_event_name(tmp_path / "runtime-a")
    other = wake_event_name(tmp_path / "runtime-b")

    assert first == replay
    assert first != other
    assert first.startswith("Local\\BDB-")


def test_cross_process_signal_is_fail_closed_off_windows(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("The Windows path is covered by the named-event integration jobs")
    assert signal_running_bridge(tmp_path / "missing-runtime") is False
