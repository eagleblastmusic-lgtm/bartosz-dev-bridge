from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bdb_bridge.git_command_transport import GitCommandTransport, read_commit_timestamp
from tests.helpers.git_control_repo import (
    SESSION_ID,
    command_payload,
    commit_and_push_commands,
    fetch_clone,
    init_control_remote,
    manifest_payload,
    write_command,
    write_manifest,
)


def _prepared_transport(tmp_path: Path) -> tuple[object, GitCommandTransport]:
    fixture = init_control_remote(tmp_path)
    write_manifest(fixture.writer, SESSION_ID, manifest_payload())
    write_command(fixture.writer, SESSION_ID, 1, command_payload(sequence=1))
    commit_and_push_commands(fixture.writer)
    fetch_clone(fixture.clone)
    return fixture, GitCommandTransport(fixture.clone)


def test_document_commit_timestamp_is_canonical_and_recoverable(tmp_path: Path) -> None:
    fixture, transport = _prepared_transport(tmp_path)

    snapshot = transport.fetch_snapshot()
    document = snapshot.commands[0]

    assert document.document_committed_at is not None
    assert document.document_committed_at.endswith("Z")
    datetime.fromisoformat(document.document_committed_at.replace("Z", "+00:00"))
    assert read_commit_timestamp(
        fixture.clone,
        document.document_commit_sha,
    ) == document.document_committed_at


def test_unchanged_snapshot_reuses_cached_documents(tmp_path: Path) -> None:
    _, transport = _prepared_transport(tmp_path)

    first = transport.fetch_snapshot()

    with patch.object(
        transport,
        "_read_document",
        wraps=transport._read_document,
    ) as read_document:
        second = transport.fetch_snapshot()

    assert second is first
    read_document.assert_not_called()


def test_fast_forward_reads_only_new_command_document(tmp_path: Path) -> None:
    fixture, transport = _prepared_transport(tmp_path)

    first = transport.fetch_snapshot()
    assert len(first.commands) == 1

    write_command(fixture.writer, SESSION_ID, 2, command_payload(sequence=2))
    new_sha = commit_and_push_commands(fixture.writer, "add command 2")

    with patch.object(
        transport,
        "_read_document",
        wraps=transport._read_document,
    ) as read_document:
        second = transport.fetch_snapshot()

    assert second.snapshot_sha == new_sha
    assert len(second.commands) == 2
    assert read_document.call_count == 1
    assert read_document.call_args.args[1].endswith("/commands/000002.json")
