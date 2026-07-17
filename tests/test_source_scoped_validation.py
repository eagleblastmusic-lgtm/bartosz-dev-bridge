from __future__ import annotations

from pathlib import Path

from bdb_bridge import (
    CommandIngestor,
    CommandSnapshot,
    CommandState,
    Journal,
    RemoteDocument,
)
from bdb_bridge.serializers import canonical_json
from tests.helpers.git_control_repo import (
    SESSION_ID,
    command_payload,
    fixed_now,
    manifest_payload,
)


def source_snapshot() -> CommandSnapshot:
    manifest = canonical_json(manifest_payload()).encode("utf-8")
    command = canonical_json(command_payload()).encode("utf-8")
    return CommandSnapshot(
        snapshot_sha="a" * 40,
        manifests=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/manifest.json",
                content=manifest,
                document_commit_sha="b" * 40,
            ),
        ),
        commands=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/commands/000001.json",
                content=command,
                document_commit_sha="c" * 40,
            ),
        ),
    )


def test_ingestor_validates_only_commands_owned_by_its_source(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    git_ingestor = CommandIngestor(journal, object(), source_id="commands")
    local_ingestor = CommandIngestor(journal, object(), source_id="local-spool")

    ingested = git_ingestor.ingest_snapshot(source_snapshot())
    assert ingested.commands_discovered == 1
    command_id = f"{SESSION_ID}:000001"
    assert journal.get_command(command_id).state is CommandState.DISCOVERED

    local_validation = local_ingestor.validate_pending()
    assert local_validation.commands_validated == 0
    assert journal.get_command(command_id).state is CommandState.DISCOVERED

    git_validation = git_ingestor.validate_pending()
    assert git_validation.commands_validated == 1
    assert journal.get_command(command_id).state is CommandState.VALIDATED
    journal.close()
