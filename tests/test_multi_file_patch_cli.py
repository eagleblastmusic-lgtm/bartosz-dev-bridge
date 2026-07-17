from __future__ import annotations

import json
from types import SimpleNamespace

from bdb_bridge import CommandState
from bdb_bridge.multi_file_patch_cli import _edit_status, _parser
from bdb_bridge.multi_file_patch_recovery_models import MultiFileCheckpointState


COMMAND_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b720:000001"
SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b720"


class FakeJournal:
    def __init__(self) -> None:
        self.closed = False
        self.command = SimpleNamespace(
            command_id=COMMAND_ID,
            session_id=SESSION_ID,
            sequence=1,
            state=CommandState.RESULT_PUBLISHED,
            command_json=json.dumps({"operation": "multi_file_patch"}),
        )

    def get_command(self, command_id: str):
        return self.command if command_id == COMMAND_ID else None

    def get_multi_file_patch_checkpoint(self, command_id: str):
        if command_id != COMMAND_ID:
            return None
        return SimpleNamespace(
            state=MultiFileCheckpointState.COMMITTED,
            checkpoint_sha256="sha256:" + "1" * 64,
            workspace_revision_before=0,
            workspace_revision_after=1,
            last_error=None,
        )

    def get_multi_file_patch_profile_run(self, command_id: str):
        return SimpleNamespace(status="success", profile_id="poc_pytest")

    def get_command_ingestion(self, command_id: str):
        if command_id != COMMAND_ID:
            return None
        return SimpleNamespace(
            created_remote_at="2026-07-15T12:00:00Z",
            first_seen_at="2026-07-15T12:00:01Z",
        )

    def list_events(self, *, command_id: str):
        if command_id != COMMAND_ID:
            return []
        return [
            SimpleNamespace(
                event_type="command.validated",
                created_at="2026-07-15T12:00:02Z",
                payload_json=None,
            ),
            SimpleNamespace(
                event_type="command.claimed",
                created_at="2026-07-15T12:00:03Z",
                payload_json=None,
            ),
        ]

    def get_result(self, command_id: str):
        if command_id != COMMAND_ID:
            return None
        return SimpleNamespace(
            status="success",
            created_at="2026-07-15T12:00:06Z",
            result_json=json.dumps(
                {
                    "started_at": "2026-07-15T12:00:04Z",
                    "finished_at": "2026-07-15T12:00:05Z",
                }
            ),
        )

    def get_outbox(self, command_id: str):
        if command_id != COMMAND_ID:
            return None
        return SimpleNamespace(
            state=SimpleNamespace(value="published"),
            published_at="2026-07-15T12:00:08Z",
        )

    def close(self) -> None:
        self.closed = True


def test_edit_status_parser_contract() -> None:
    args = _parser().parse_args(
        [
            "bridge",
            "edit",
            "status",
            "--config",
            "bridge.json",
            "--command-id",
            COMMAND_ID,
            "--json",
        ]
    )
    assert args.bridge_command == "edit"
    assert args.edit_command == "status"
    assert args.command_id == COMMAND_ID
    assert args.json is True


def test_edit_status_reports_durable_batch_state_and_timing(monkeypatch, capsys) -> None:
    fake = FakeJournal()
    monkeypatch.setattr(
        "bdb_bridge.multi_file_patch_cli.Journal.open",
        lambda path: fake,
    )
    assert _edit_status(SimpleNamespace(journal_path="journal.db"), COMMAND_ID, True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "checkpoint_sha256": "sha256:" + "1" * 64,
        "checkpoint_state": "committed",
        "command_id": COMMAND_ID,
        "command_state": "result_published",
        "last_error": None,
        "outbox_state": "published",
        "profile_id": "poc_pytest",
        "profile_status": "success",
        "result_status": "success",
        "sequence": 1,
        "session_id": SESSION_ID,
        "timing": {
            "durations_ms": {
                "end_to_end_ms": 8000.0,
                "execution_ms": 1000.0,
                "inbound_transport_ms": 1000.0,
                "pre_execution_ms": 1000.0,
                "result_publication_ms": 2000.0,
                "result_staging_ms": 1000.0,
                "scheduler_queue_ms": 1000.0,
                "validation_ms": 1000.0,
            },
            "timestamps": {
                "claimed_at": "2026-07-15T12:00:03Z",
                "execution_finished_at": "2026-07-15T12:00:05Z",
                "execution_started_at": "2026-07-15T12:00:04Z",
                "first_seen_at": "2026-07-15T12:00:01Z",
                "remote_created_at": "2026-07-15T12:00:00Z",
                "result_published_at": "2026-07-15T12:00:08Z",
                "result_staged_at": "2026-07-15T12:00:06Z",
                "validated_at": "2026-07-15T12:00:02Z",
            },
        },
        "workspace_revision_after": 1,
        "workspace_revision_before": 0,
    }
    assert fake.closed is True
