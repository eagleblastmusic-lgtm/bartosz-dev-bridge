from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_bridge import BridgeError, CommandIngestor, CommandState, Journal
from bdb_bridge.local_spool_transport import (
    LOCAL_ENVELOPE_SCHEMA,
    LocalSpoolTransport,
    LocalSpoolWriter,
)
from bdb_bridge.priority_ingestion import PriorityCommandIngestor
from tests.helpers.git_control_repo import (
    CREATED_AT,
    SESSION_ID,
    command_payload,
    fixed_now,
    manifest_payload,
)


def envelope(*, command: dict | None = None, manifest: dict | None = None) -> dict:
    return {
        "schema": LOCAL_ENVELOPE_SCHEMA,
        "submitted_at": CREATED_AT,
        "manifest": manifest or manifest_payload(),
        "command": command or command_payload(),
    }


def test_writer_publishes_atomically_and_transport_builds_canonical_snapshot(tmp_path: Path) -> None:
    inbox = tmp_path / "spool" / "inbox"
    destination = LocalSpoolWriter(inbox).submit(envelope(), filename="action-000001.json")

    assert destination.exists()
    assert list(inbox.glob("*.tmp")) == []
    snapshot = LocalSpoolTransport(inbox).fetch_snapshot()

    assert len(snapshot.snapshot_sha) == 40
    assert [item.path for item in snapshot.manifests] == [
        f"sessions/{SESSION_ID}/manifest.json"
    ]
    assert [item.path for item in snapshot.commands] == [
        f"sessions/{SESSION_ID}/commands/000001.json"
    ]
    assert snapshot.commands[0].document_committed_at == CREATED_AT


def test_transport_ignores_unpublished_temp_files(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / ".partial.json.1.tmp").write_text("{", encoding="utf-8")

    snapshot = LocalSpoolTransport(inbox).fetch_snapshot()

    assert snapshot.manifests == ()
    assert snapshot.commands == ()


def test_writer_is_idempotent_for_exact_bytes_and_rejects_collision(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    writer = LocalSpoolWriter(inbox)
    first = writer.submit(envelope(), filename="action.json")
    replay = writer.submit(envelope(), filename="action.json")
    assert replay == first

    changed = envelope(command=command_payload(payload={"path": "tests/test_clamp.py"}))
    with pytest.raises(BridgeError) as exc:
        writer.submit(changed, filename="action.json")
    assert exc.value.code == "journal_conflict"


def test_local_snapshot_reuses_durable_ingestion_and_validation(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    LocalSpoolWriter(inbox).submit(envelope(), filename="action.json")
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)

    report = CommandIngestor(
        journal,
        LocalSpoolTransport(inbox),
        source_id="local-spool",
    ).poll_once()

    assert report.error_code is None
    assert report.ingestion is not None
    assert report.ingestion.commands_validated == 1
    command = journal.get_command(f"{SESSION_ID}:000001")
    assert command is not None
    assert command.state is CommandState.VALIDATED
    metadata = journal.get_command_ingestion(command.command_id)
    assert metadata is not None
    assert metadata.source_id == "local-spool"
    journal.close()


def test_mismatched_session_ids_fail_closed(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    bad = envelope()
    bad["command"] = {
        **command_payload(),
        "session_id": "028f3f66-6cb3-4f66-9f2e-3d7647d1b709",
    }
    (inbox).mkdir()
    (inbox / "bad.json").write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(BridgeError) as exc:
        LocalSpoolTransport(inbox).fetch_snapshot()
    assert exc.value.code == "invalid_payload"


class StubIngestor:
    def __init__(self, report: object) -> None:
        self.report = report
        self.calls = 0

    def poll_once(self):
        self.calls += 1
        return self.report


def test_priority_ingestor_skips_github_after_local_durable_work(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    LocalSpoolWriter(inbox).submit(envelope(), filename="action.json")
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    local = CommandIngestor(journal, LocalSpoolTransport(inbox), source_id="local-spool")

    class ForbiddenFallback:
        calls = 0

        def poll_once(self):
            self.calls += 1
            raise AssertionError("Git fallback must not run after local progress")

    fallback = ForbiddenFallback()
    report = PriorityCommandIngestor(local, fallback).poll_once()

    assert report.ingestion is not None
    assert report.ingestion.commands_validated == 1
    assert fallback.calls == 0
    journal.close()
