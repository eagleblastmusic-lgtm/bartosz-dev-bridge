from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    BridgeErrorCode,
    CommandIngestor,
    CommandSnapshot,
    CommandState,
    Journal,
    RemoteDocument,
    SessionState,
    SingleQueueScheduler,
    canonical_json,
    sha256_text,
)
from bdb_bridge.protocol import BridgeError as ProtocolBridgeError
from tests.helpers.git_control_repo import (
    BASE_SHA,
    CREATED_AT,
    EXPIRED_AT,
    EXPIRES_AT,
    FIXED_NOW,
    SESSION_ID,
    SESSION_ID_B,
    command_payload,
    fixed_now,
    manifest_payload,
)


class FakeTransport:
    def __init__(self, snapshot: CommandSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def fetch_snapshot(self) -> CommandSnapshot:
        self.calls += 1
        return self._snapshot


def open_journal(tmp_path: Path) -> Journal:
    return Journal.open(tmp_path / "journal.db", now_fn=fixed_now)


def make_snapshot(
    *,
    session_id: str = SESSION_ID,
    sequences: tuple[int, ...] = (1,),
    manifest: dict | None = None,
    command_overrides: dict[int, dict] | None = None,
    snapshot_sha: str = "s" * 40,
) -> CommandSnapshot:
    manifest_body = manifest or manifest_payload(session_id=session_id)
    manifest_text = json.dumps(manifest_body)
    manifests = [
        RemoteDocument(
            path=f"sessions/{session_id}/manifest.json",
            content=manifest_text,
            document_commit_sha="m" * 40,
        )
    ]
    commands = []
    for sequence in sequences:
        payload = command_payload(session_id=session_id, sequence=sequence)
        if command_overrides and sequence in command_overrides:
            payload.update(command_overrides[sequence])
        commands.append(
            RemoteDocument(
                path=f"sessions/{session_id}/commands/{sequence:06d}.json",
                content=json.dumps(payload),
                document_commit_sha=f"{sequence:040x}",
            )
        )
    return CommandSnapshot(snapshot_sha=snapshot_sha, manifests=tuple(manifests), commands=tuple(commands))


def ingest(journal: Journal, snapshot: CommandSnapshot) -> CommandIngestor:
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    ingestor.ingest_snapshot(snapshot)
    ingestor.validate_pending()
    return ingestor


def test_identical_snapshot_ingested_ten_times(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot()
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    for _ in range(10):
        ingestor.ingest_snapshot(snapshot)
    assert journal.get_session(SESSION_ID) is not None
    assert journal.get_command(f"{SESSION_ID}:000001") is not None
    events = journal.list_events(session_id=SESSION_ID)
    discovered = [event for event in events if event.event_type == "command.discovered"]
    assert len(discovered) == 1
    journal.close()


def test_restart_after_discovered_resumes_validation(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    snapshot = make_snapshot()
    journal = Journal.open(path, now_fn=fixed_now)
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    ingestor.ingest_snapshot(snapshot)
    assert journal.get_command(f"{SESSION_ID}:000001").state == CommandState.DISCOVERED
    journal.close()

    journal = Journal.open(path, now_fn=fixed_now)
    CommandIngestor(journal, FakeTransport(snapshot)).validate_pending()
    assert journal.get_command(f"{SESSION_ID}:000001").state == CommandState.VALIDATED
    journal.close()


def test_manifest_collision_is_blocking(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    first = make_snapshot()
    ingest(journal, first)
    mutated = make_snapshot(
        manifest={
            **manifest_payload(),
            "repository_id": "other-repo",
        },
        snapshot_sha="t" * 40,
    )
    ingestor = CommandIngestor(journal, FakeTransport(mutated))
    with pytest.raises(BridgeError) as exc:
        ingestor.ingest_snapshot(mutated)
    assert exc.value.code == BridgeErrorCode.SESSION_ID_COLLISION.value
    assert journal.has_blocking_ingestion_issues()
    journal.close()


def test_command_collision_whitespace_mutation(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    first = make_snapshot()
    ingest(journal, first)
    payload = command_payload(sequence=1)
    whitespace_variant = json.dumps(payload) + " "
    mutated = CommandSnapshot(
        snapshot_sha="u" * 40,
        manifests=first.manifests,
        commands=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/commands/000001.json",
                content=whitespace_variant,
                document_commit_sha="n" * 40,
            ),
        ),
    )
    ingestor = CommandIngestor(journal, FakeTransport(mutated))
    with pytest.raises(BridgeError) as exc:
        ingestor.ingest_snapshot(mutated)
    assert exc.value.code == BridgeErrorCode.COMMAND_ID_COLLISION.value
    journal.close()


def test_unsupported_schema_rejected(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot(
        command_overrides={1: {"schema_version": "9.9"}},
    )
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    ingestor.ingest_snapshot(snapshot)
    ingestor.validate_pending()
    command = journal.get_command(f"{SESSION_ID}:000001")
    assert command.state == CommandState.REJECTED
    journal.close()


def test_expired_command_at_boundary(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot(
        command_overrides={
            1: {
                "created_at": "2026-07-15T05:00:00Z",
                "expires_at": FIXED_NOW,
            }
        },
    )
    ingest(journal, snapshot)
    command = journal.get_command(f"{SESSION_ID}:000001")
    assert command.state == CommandState.EXPIRED
    journal.close()


def test_expired_manifest_aborts_session(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = make_snapshot(
        manifest=manifest_payload(expires_at=EXPIRED_AT, created_at="2026-07-13T08:00:00Z"),
    )
    ingest(journal, snapshot)
    session = journal.get_session(SESSION_ID)
    assert session.state == SessionState.ABORTED
    journal.close()


def test_sequence_gap_then_later_sequence_one(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    only_two = make_snapshot(sequences=(2,))
    ingestor = CommandIngestor(journal, FakeTransport(only_two))
    ingestor.ingest_snapshot(only_two)
    ingestor.validate_pending()
    command_two = journal.get_command(f"{SESSION_ID}:000002")
    assert command_two is not None
    assert command_two.state == CommandState.DISCOVERED

    with_seq1 = make_snapshot(sequences=(1, 2), snapshot_sha="v" * 40)
    ingestor.ingest_snapshot(with_seq1)
    ingestor.validate_pending()
    assert journal.get_command(f"{SESSION_ID}:000001").state == CommandState.VALIDATED
    assert journal.get_command(f"{SESSION_ID}:000002").state == CommandState.VALIDATED
    journal.close()


def test_command_without_manifest_not_claimed(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    command_only = CommandSnapshot(
        snapshot_sha="w" * 40,
        manifests=(),
        commands=(make_snapshot(sequences=(1,)).commands[0],),
    )
    ingestor = CommandIngestor(journal, FakeTransport(command_only))
    ingestor.ingest_snapshot(command_only)
    assert journal.get_command(f"{SESSION_ID}:000001") is None
    scheduler = SingleQueueScheduler(journal)
    assert scheduler.claim_next() is None
    journal.close()


def test_ingestor_does_not_execute_commands(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    ingest(journal, make_snapshot())
    scheduler = SingleQueueScheduler(journal)
    claimed = scheduler.claim_next()
    assert claimed is not None
    assert claimed.state == CommandState.CLAIMED
    journal.close()


def test_malformed_json_records_issue(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snapshot = CommandSnapshot(
        snapshot_sha="x" * 40,
        manifests=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/manifest.json",
                content="{bad",
                document_commit_sha="y" * 40,
            ),
        ),
        commands=(),
    )
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    report = ingestor.ingest_snapshot(snapshot)
    assert report.issues_recorded == 1
    journal.close()


def test_two_sessions_ingested(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    snap_a = make_snapshot(session_id=SESSION_ID, snapshot_sha="a" * 40)
    snap_b = make_snapshot(session_id=SESSION_ID_B, snapshot_sha="b" * 40)
    combined = CommandSnapshot(
        snapshot_sha="z" * 40,
        manifests=snap_a.manifests + snap_b.manifests,
        commands=snap_a.commands + snap_b.commands,
    )
    ingest(journal, combined)
    assert journal.get_session(SESSION_ID) is not None
    assert journal.get_session(SESSION_ID_B) is not None
    journal.close()
