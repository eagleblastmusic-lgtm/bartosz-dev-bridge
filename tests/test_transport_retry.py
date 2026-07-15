from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bdb_bridge import BridgeErrorCode, CommandIngestor, CommandSnapshot, Journal, RemoteDocument
from bdb_bridge.journal_ingestion import compute_backoff_delay, get_ingestion_source
from bdb_bridge.protocol import BridgeError
from tests.helpers.git_control_repo import (
    BASE_SHA,
    CREATED_AT,
    EXPIRES_AT,
    FIXED_NOW,
    SESSION_ID,
    command_payload,
    fixed_now,
    manifest_payload,
)


class FakeTransport:
    def __init__(self, snapshots: list[CommandSnapshot], *, fail_times: int = 0) -> None:
        self._snapshots = list(snapshots)
        self._fail_times = fail_times
        self.calls = 0

    def fetch_snapshot(self) -> CommandSnapshot:
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "network down")
        if not self._snapshots:
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "empty")
        return self._snapshots.pop(0)


def open_journal(tmp_path: Path) -> Journal:
    return Journal.open(tmp_path / "journal.db", now_fn=fixed_now)


def snapshot_from_docs(
    *,
    snapshot_sha: str = "e" * 40,
    manifests: list[RemoteDocument] | None = None,
    commands: list[RemoteDocument] | None = None,
) -> CommandSnapshot:
    return CommandSnapshot(
        snapshot_sha=snapshot_sha,
        manifests=tuple(manifests or []),
        commands=tuple(commands or []),
    )


def manifest_doc(session_id: str = SESSION_ID, content: str | bytes | None = None) -> RemoteDocument:
    payload = manifest_payload(session_id=session_id)
    if content is None:
        b_content = __import__("json").dumps(payload).encode("utf-8")
    elif isinstance(content, str):
        b_content = content.encode("utf-8")
    else:
        b_content = content
    return RemoteDocument(
        path=f"sessions/{session_id}/manifest.json",
        content=b_content,
        document_commit_sha="f" * 40,
    )


def command_doc(sequence: int = 1, session_id: str = SESSION_ID, content: str | bytes | None = None) -> RemoteDocument:
    payload = command_payload(session_id=session_id, sequence=sequence)
    if content is None:
        b_content = __import__("json").dumps(payload).encode("utf-8")
    elif isinstance(content, str):
        b_content = content.encode("utf-8")
    else:
        b_content = content
    return RemoteDocument(
        path=f"sessions/{session_id}/commands/{sequence:06d}.json",
        content=b_content,
        document_commit_sha=f"{sequence:040d}",
    )


def test_first_failure_sets_attempt_one(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    transport = FakeTransport([], fail_times=1)
    ingestor = CommandIngestor(journal, transport, now_fn=fixed_now)
    report = ingestor.poll_once()
    assert report.transport_called is True
    assert report.transport_succeeded is False
    source = get_ingestion_source(journal, "commands")
    assert source.attempt_count == 1
    journal.close()


def test_exponential_backoff_delay() -> None:
    assert compute_backoff_delay(1) == 1.0
    assert compute_backoff_delay(2) == 2.0
    assert compute_backoff_delay(3) == 4.0
    assert compute_backoff_delay(10, max_delay=60.0) == 60.0


def test_retry_before_next_attempt_skips_transport(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    journal.record_transport_failure("commands", "down")
    transport = FakeTransport([snapshot_from_docs(manifests=[manifest_doc()])])
    ingestor = CommandIngestor(journal, transport, now_fn=fixed_now)
    report = ingestor.poll_once()
    assert report.transport_skipped is True
    assert transport.calls == 0
    journal.close()


def test_retry_due_at_next_attempt_calls_transport(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    journal.record_transport_failure("commands", "down")
    source = journal.get_ingestion_source("commands")
    assert source.next_attempt_at is not None

    def later_now() -> str:
        parsed = datetime.fromisoformat(source.next_attempt_at.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    transport = FakeTransport([snapshot_from_docs(manifests=[manifest_doc()])])
    ingestor = CommandIngestor(journal, transport, now_fn=later_now)
    report = ingestor.poll_once()
    assert report.transport_skipped is False
    assert transport.calls == 1
    journal.close()


def test_reopen_preserves_retry_state(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)
    journal.record_transport_failure("commands", "down")
    journal.close()

    journal = Journal.open(path, now_fn=fixed_now)
    source = journal.get_ingestion_source("commands")
    assert source.attempt_count == 1
    assert source.next_attempt_at is not None
    journal.close()


def test_success_resets_failure_state(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    journal.record_transport_failure("commands", "down")
    source = journal.get_ingestion_source("commands")
    assert source.next_attempt_at is not None

    def later_now() -> str:
        return source.next_attempt_at

    transport = FakeTransport([snapshot_from_docs(manifests=[manifest_doc()])])
    ingestor = CommandIngestor(journal, transport, now_fn=later_now)
    ingestor.poll_once()
    source = journal.get_ingestion_source("commands")
    assert source.attempt_count == 0
    assert source.last_error is None
    journal.close()


def test_last_error_is_truncated(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    journal.record_transport_failure("commands", "x" * 1000)
    source = journal.get_ingestion_source("commands")
    assert source.last_error is not None
    assert len(source.last_error) <= 512
    journal.close()


def test_validation_error_does_not_increment_transport_retry(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    bad_manifest = manifest_doc(content="{not-json")
    transport = FakeTransport([snapshot_from_docs(manifests=[bad_manifest])])
    ingestor = CommandIngestor(journal, transport)
    ingestor.poll_once()
    source = journal.get_ingestion_source("commands")
    assert source.attempt_count == 0
    journal.close()
