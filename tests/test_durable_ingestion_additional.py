import json
import pathlib
from pathlib import Path
import pytest

from bdb_bridge.ingestion import CommandIngestor, calculate_raw_sha256
from bdb_bridge.journal import Journal
from bdb_bridge.journal_ingestion import get_session_ingestion, get_command_ingestion, CollisionError, _row_to_ingestion_issue
from bdb_bridge.models import BridgeErrorCode, CommandState, SessionState
from bdb_bridge.protocol import BridgeError
from bdb_bridge.serializers import sha256_text
from bdb_bridge.transport import CommandSnapshot, RemoteDocument, CommandTransport

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"


class FakeTransport(CommandTransport):
    def __init__(self, snapshot: CommandSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def fetch_snapshot(self) -> CommandSnapshot:
        self.calls += 1
        return self._snapshot


def fixed_now() -> str:
    return "2026-07-15T08:00:00Z"


def open_journal(tmp_path: Path) -> Journal:
    return Journal.open(tmp_path / "journal.db", now_fn=fixed_now)


def manifest_payload(session_id: str = SESSION_ID) -> dict:
    return {
        "schema_version": "1.1",
        "session_id": session_id,
        "repository_id": "origin",
        "base_sha": "a" * 40,
        "created_at": fixed_now(),
        "expires_at": "2026-07-15T09:00:00Z",
        "allowed_paths": ["*.py"],
    }


def command_payload(session_id: str = SESSION_ID, sequence: int = 1) -> dict:
    return {
        "schema_version": "1.1",
        "session_id": session_id,
        "command_id": f"{session_id}:{sequence:06d}",
        "sequence": sequence,
        "created_at": fixed_now(),
        "expires_at": "2026-07-15T09:00:00Z",
        "expected_revision": 0,
        "operation": "open_read",
        "payload": {"path": "main.py", "start_line": 1, "end_line": 10},
    }


def make_snapshot(
    *,
    session_id: str = SESSION_ID,
    sequences: tuple[int, ...] = (1,),
    manifest: dict | None = None,
    command_overrides: dict[int, dict] | None = None,
    snapshot_sha: str = "e" * 40,
) -> CommandSnapshot:
    manifest_body = manifest or manifest_payload(session_id=session_id)
    manifest_bytes = json.dumps(manifest_body).encode("utf-8")
    manifests = [
        RemoteDocument(
            path=f"sessions/{session_id}/manifest.json",
            content=manifest_bytes,
            document_commit_sha="f" * 40,
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
                content=json.dumps(payload).encode("utf-8"),
                document_commit_sha=f"{sequence:040x}",
            )
        )
    return CommandSnapshot(snapshot_sha=snapshot_sha, manifests=tuple(manifests), commands=tuple(commands))


def test_poll_once_blocking_collision(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)

    # 1. Ingest a valid session & command 1
    snapshot1 = make_snapshot(session_id=SESSION_ID, sequences=(1,))
    ingestor = CommandIngestor(journal, FakeTransport(snapshot1))
    report1 = ingestor.poll_once()
    assert report1.error_code is None
    assert report1.ingestion is not None
    assert report1.ingestion.commands_validated == 1

    # 2. Ingest command 1 again but with different content (sequence collision)
    # and a new valid command 2.
    overrides = {1: {"operation": "replace_exact_and_test"}}
    snapshot2 = make_snapshot(session_id=SESSION_ID, sequences=(1, 2), command_overrides=overrides)
    ingestor2 = CommandIngestor(journal, FakeTransport(snapshot2))
    report2 = ingestor2.poll_once()

    # The exception should be caught, and validate_pending still runs for valid commands
    assert report2.error_code == BridgeErrorCode.INGESTION_BLOCKED.value
    assert report2.ingestion is not None
    assert report2.ingestion.blocking_issues is True
    # Command 2 is validated successfully!
    assert report2.ingestion.commands_validated == 1

    # Transport attempt count remains 0 because transport succeeded (no failure recorded)
    src = journal.get_ingestion_source("commands")
    assert src.attempt_count == 0

    journal.close()


def test_reopen_blocking_issues(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)

    # 1. Record session manifest
    snapshot1 = make_snapshot(session_id=SESSION_ID, sequences=(1,))
    ingestor = CommandIngestor(journal, FakeTransport(snapshot1))
    ingestor.poll_once()

    # 2. Manifest collision with different repository_id
    snapshot2 = make_snapshot(
        session_id=SESSION_ID,
        sequences=(2,),
        manifest={**manifest_payload(session_id=SESSION_ID), "repository_id": "other_repo"},
    )
    ingestor2 = CommandIngestor(journal, FakeTransport(snapshot2))
    report2 = ingestor2.poll_once()
    assert report2.error_code == BridgeErrorCode.INGESTION_BLOCKED.value
    assert journal.has_blocking_ingestion_issues() is True
    journal.close()

    # Reopen and verify it still has blocking issues
    journal_reopen = Journal.open(path, now_fn=fixed_now)
    assert journal_reopen.has_blocking_ingestion_issues() is True

    # Events table must have ingestion.blocked event
    events = journal_reopen.list_events(session_id=SESSION_ID)
    blocked_events = [e for e in events if e.event_type == "ingestion.blocked"]
    assert len(blocked_events) > 0
    journal_reopen.close()


def test_v1_upgrade_session_ingestion_check(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)

    # Insert session directly into sessions table without session_ingestion (upgraded v1 db)
    with journal._transaction():
        journal._connection.execute(
            "INSERT INTO sessions (session_id, repository_id, base_sha, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("018f3f66-6cb3-4f66-9f2e-3d7647d1b701", "origin", "a" * 40, SessionState.CREATED.value, fixed_now(), fixed_now()),
        )

    # Record command for this session. Since session_ingestion is None, it must go to staging
    raw_content = json.dumps(command_payload(session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701", sequence=1)).encode("utf-8")
    rec, created, _ = journal.record_ingested_command(
        source_id="commands",
        snapshot_sha="a" * 40,
        source_path="sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/commands/000001.json",
        session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
        sequence=1,
        document_commit_sha="b" * 40,
        raw_content=raw_content,
        raw_sha256_value=calculate_raw_sha256(raw_content),
    )
    assert rec is None  # staged!
    assert created is True

    # Verify it is in pending_command_documents
    staged = journal._connection.execute(
        "SELECT 1 FROM pending_command_documents WHERE session_id = '018f3f66-6cb3-4f66-9f2e-3d7647d1b701'"
    ).fetchone()
    assert staged is not None

    # Now record the session manifest, which should promote the staged command
    manifest_body = manifest_payload(session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701")
    journal.record_session_manifest(
        source_id="commands",
        snapshot_sha="a" * 40,
        source_path="sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/manifest.json",
        session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
        manifest_commit_sha="b" * 40,
        raw_content=json.dumps(manifest_body),
        manifest_json=json.dumps(manifest_body),
        manifest_sha256=sha256_text(json.dumps(manifest_body)),
        raw_sha256=calculate_raw_sha256(json.dumps(manifest_body).encode("utf-8")),
        repository_id="origin",
        base_sha="a" * 40,
        created_remote_at=fixed_now(),
        expires_at=fixed_now(),
    )

    # Verify it is promoted and no longer in staging
    staged_after = journal._connection.execute(
        "SELECT 1 FROM pending_command_documents WHERE session_id = '018f3f66-6cb3-4f66-9f2e-3d7647d1b701'"
    ).fetchone()
    assert staged_after is None
    assert journal.get_command("018f3f66-6cb3-4f66-9f2e-3d7647d1b701:000001") is not None

    journal.close()


def test_staged_content_exact_bytes_consistency(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    journal = Journal.open(path, now_fn=fixed_now)

    # Exact bytes containing CRLF, null bytes, BOM, etc.
    exact_bytes = b"\xef\xbb\xbf{\r\n  \"sequence\": 1,\r\n  \"content\": \"test\x00\"\r\n}"

    journal.record_ingested_command(
        source_id="commands",
        snapshot_sha="a" * 40,
        source_path="sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/commands/000001.json",
        session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
        sequence=1,
        document_commit_sha="b" * 40,
        raw_content=exact_bytes,
        raw_sha256_value=calculate_raw_sha256(exact_bytes),
    )
    journal.close()

    # Reopen and read from staging to verify exact bytes
    journal_reopen = Journal.open(path, now_fn=fixed_now)
    row = journal_reopen._connection.execute(
        "SELECT content FROM pending_command_documents WHERE session_id = '018f3f66-6cb3-4f66-9f2e-3d7647d1b701' AND sequence = 1"
    ).fetchone()
    assert row is not None
    assert row[0] == exact_bytes

    # Now promote it
    manifest_body = manifest_payload(session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701")
    journal_reopen.record_session_manifest(
        source_id="commands",
        snapshot_sha="a" * 40,
        source_path="sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/manifest.json",
        session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
        manifest_commit_sha="b" * 40,
        raw_content=json.dumps(manifest_body),
        manifest_json=json.dumps(manifest_body),
        manifest_sha256=sha256_text(json.dumps(manifest_body)),
        raw_sha256=calculate_raw_sha256(json.dumps(manifest_body).encode("utf-8")),
        repository_id="origin",
        base_sha="a" * 40,
        created_remote_at=fixed_now(),
        expires_at=fixed_now(),
    )

    cmd = journal_reopen.get_command("018f3f66-6cb3-4f66-9f2e-3d7647d1b701:000001")
    assert cmd is not None
    assert cmd.command_json == exact_bytes.decode("utf-8")

    journal_reopen.close()


def test_provenance_isolation_and_missing_metadata(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)

    with journal._transaction():
        journal._connection.execute(
            "INSERT INTO sessions (session_id, repository_id, base_sha, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("018f3f66-6cb3-4f66-9f2e-3d7647d1b701", "origin", "a" * 40, SessionState.CREATED.value, fixed_now(), fixed_now()),
        )
        journal._connection.execute(
            """
            INSERT INTO session_ingestion (
                session_id, source_path, manifest_commit_sha, raw_sha256, manifest_sha256,
                manifest_json, created_remote_at, expires_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
                "sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/manifest.json",
                "b" * 40,
                "raw_hash",
                "manifest_hash",
                "manifest_json",
                fixed_now(),
                "2026-07-15T09:00:00Z",
                fixed_now(),
                fixed_now(),
            ),
        )
        journal._connection.execute(
            """
            INSERT INTO commands (
                command_id, session_id, sequence, command_sha256, command_json,
                command_commit_sha, state, expected_revision, expected_state_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                "018f3f66-6cb3-4f66-9f2e-3d7647d1b701:000001",
                "018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
                1,
                "c_hash",
                json.dumps(command_payload(session_id="018f3f66-6cb3-4f66-9f2e-3d7647d1b701", sequence=1)),
                "c_commit",
                CommandState.DISCOVERED.value,
                fixed_now(),
                fixed_now(),
            ),
        )

    # validate_pending must raise JOURNAL_CONFLICT when command_ingestion metadata is missing
    ingestor = CommandIngestor(journal, FakeTransport(CommandSnapshot("a" * 40, (), ())))
    with pytest.raises(BridgeError) as excinfo:
        ingestor.validate_pending()
    assert excinfo.value.code == BridgeErrorCode.JOURNAL_CONFLICT.value

    journal.close()


def test_generic_malformed_transport_fails_with_bridge_error(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)

    # 1. Non-hex snapshot_sha
    snapshot = CommandSnapshot(snapshot_sha="xyz", manifests=(), commands=())
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    with pytest.raises(BridgeError) as excinfo:
        ingestor.poll_once()
    assert excinfo.value.code == BridgeErrorCode.INVALID_PAYLOAD.value

    # 2. Non-hex document_commit_sha in manifest
    snapshot = CommandSnapshot(
        snapshot_sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        manifests=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/manifest.json",
                content=json.dumps(manifest_payload()).encode("utf-8"),
                document_commit_sha="xyz",
            ),
        ),
        commands=(),
    )
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    with pytest.raises(BridgeError) as excinfo:
        ingestor.poll_once()
    assert excinfo.value.code == BridgeErrorCode.INVALID_PAYLOAD.value

    # 3. Bad type content (not bytes)
    snapshot = CommandSnapshot(
        snapshot_sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        manifests=(
            RemoteDocument(
                path=f"sessions/{SESSION_ID}/manifest.json",
                content="string content",  # type: ignore
                document_commit_sha="ffffffffffffffffffffffffffffffffffffffff",
            ),
        ),
        commands=(),
    )
    ingestor = CommandIngestor(journal, FakeTransport(snapshot))
    with pytest.raises(BridgeError) as excinfo:
        ingestor.poll_once()
    assert excinfo.value.code == BridgeErrorCode.INVALID_PAYLOAD.value

    journal.close()


def test_registry_schema_verification(tmp_path: Path) -> None:
    journal = open_journal(tmp_path)
    # Verify that the actual tables in the database match exactly the registered tables
    # by checking that the set of user tables has no unknown tables relative to JOURNAL_TABLES
    user_tables = set()
    for row in journal._connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        user_tables.add(row[0])
    # SQLite system tables like sqlite_sequence are ignored
    user_tables.discard("sqlite_sequence")

    from bdb_bridge.migrations import JOURNAL_TABLES
    unknown = user_tables - JOURNAL_TABLES
    assert not unknown, f"Found unregistered tables: {unknown}"
    journal.close()


def get_ingestion_issues(journal: Journal) -> list[IngestionIssue]:
    rows = journal._connection.execute(
        """
        SELECT issue_id, source_id, source_path, snapshot_sha, document_commit_sha,
               raw_sha256, session_id, command_id, error_code, detail, blocking, created_at
        FROM ingestion_issues
        """
    ).fetchall()
    return [_row_to_ingestion_issue(row) for row in rows]


@pytest.mark.parametrize("collision_type", [
    "manifest_collision",
    "v1_session_collision",
    "staged_command_collision",
    "direct_command_collision",
    "staged_promotion_collision"
])
def test_single_owner_of_blocked_event(tmp_path: Path, collision_type: str) -> None:
    db_path = tmp_path / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    session_id = SESSION_ID
    # Prepare base data depending on collision type
    if collision_type == "manifest_collision":
        manifest1 = manifest_payload(session_id)
        journal.record_session_manifest(
            source_id="commands",
            snapshot_sha="a" * 40,
            source_path=f"sessions/{session_id}/manifest.json",
            session_id=session_id,
            manifest_commit_sha="b" * 40,
            raw_content=json.dumps(manifest1),
            manifest_json=json.dumps(manifest1),
            manifest_sha256=sha256_text(json.dumps(manifest1)),
            raw_sha256="c" * 64,
            repository_id="origin",
            base_sha="a" * 40,
            created_remote_at=fixed_now(),
            expires_at="2026-07-15T09:00:00Z",
        )

        manifest2 = {**manifest1, "repository_id": "other"}
        def trigger_collision():
            with pytest.raises(CollisionError):
                journal.record_session_manifest(
                    source_id="commands",
                    snapshot_sha="a" * 40,
                    source_path=f"sessions/{session_id}/manifest.json",
                    session_id=session_id,
                    manifest_commit_sha="b" * 40,
                    raw_content=json.dumps(manifest2),
                    manifest_json=json.dumps(manifest2),
                    manifest_sha256=sha256_text(json.dumps(manifest2)),
                    raw_sha256="d" * 64,
                    repository_id="other",
                    base_sha="a" * 40,
                    created_remote_at=fixed_now(),
                    expires_at="2026-07-15T09:00:00Z",
                )

    elif collision_type == "v1_session_collision":
        with journal._transaction():
            journal._connection.execute(
                "INSERT INTO sessions (session_id, repository_id, base_sha, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, "origin", "a" * 40, SessionState.CREATED.value, fixed_now(), fixed_now())
            )

        manifest = manifest_payload(session_id)
        manifest["repository_id"] = "other"
        def trigger_collision():
            with pytest.raises(CollisionError):
                journal.record_session_manifest(
                    source_id="commands",
                    snapshot_sha="a" * 40,
                    source_path=f"sessions/{session_id}/manifest.json",
                    session_id=session_id,
                    manifest_commit_sha="b" * 40,
                    raw_content=json.dumps(manifest),
                    manifest_json=json.dumps(manifest),
                    manifest_sha256=sha256_text(json.dumps(manifest)),
                    raw_sha256="c" * 64,
                    repository_id="other",
                    base_sha="a" * 40,
                    created_remote_at=fixed_now(),
                    expires_at="2026-07-15T09:00:00Z",
                )

    elif collision_type == "staged_command_collision":
        cmd1 = json.dumps(command_payload(session_id, 1)).encode("utf-8")
        journal.record_ingested_command(
            source_id="commands",
            snapshot_sha="a" * 40,
            source_path=f"sessions/{session_id}/commands/000001.json",
            session_id=session_id,
            sequence=1,
            document_commit_sha="b" * 40,
            raw_content=cmd1,
            raw_sha256_value="c" * 64,
        )

        cmd2 = json.dumps({**command_payload(session_id, 1), "operation": "other"}).encode("utf-8")
        def trigger_collision():
            with pytest.raises(CollisionError):
                journal.record_ingested_command(
                    source_id="commands",
                    snapshot_sha="a" * 40,
                    source_path=f"sessions/{session_id}/commands/000001.json",
                    session_id=session_id,
                    sequence=1,
                    document_commit_sha="b" * 40,
                    raw_content=cmd2,
                    raw_sha256_value="d" * 64,
                )

    elif collision_type == "direct_command_collision":
        manifest = manifest_payload(session_id)
        journal.record_session_manifest(
            source_id="commands",
            snapshot_sha="a" * 40,
            source_path=f"sessions/{session_id}/manifest.json",
            session_id=session_id,
            manifest_commit_sha="b" * 40,
            raw_content=json.dumps(manifest),
            manifest_json=json.dumps(manifest),
            manifest_sha256=sha256_text(json.dumps(manifest)),
            raw_sha256="c" * 64,
            repository_id="origin",
            base_sha="a" * 40,
            created_remote_at=fixed_now(),
            expires_at="2026-07-15T09:00:00Z",
        )

        cmd1 = json.dumps(command_payload(session_id, 1)).encode("utf-8")
        journal.record_ingested_command(
            source_id="commands",
            snapshot_sha="a" * 40,
            source_path=f"sessions/{session_id}/commands/000001.json",
            session_id=session_id,
            sequence=1,
            document_commit_sha="b" * 40,
            raw_content=cmd1,
            raw_sha256_value="c" * 64,
        )

        cmd2 = json.dumps({**command_payload(session_id, 1), "operation": "other"}).encode("utf-8")
        def trigger_collision():
            with pytest.raises(CollisionError):
                journal.record_ingested_command(
                    source_id="commands",
                    snapshot_sha="a" * 40,
                    source_path=f"sessions/{session_id}/commands/000001.json",
                    session_id=session_id,
                    sequence=1,
                    document_commit_sha="d" * 40,
                    raw_content=cmd2,
                    raw_sha256_value="e" * 64,
                )

    elif collision_type == "staged_promotion_collision":
        cmd1 = json.dumps(command_payload(session_id, 1)).encode("utf-8")
        journal.record_ingested_command(
            source_id="commands",
            snapshot_sha="a" * 40,
            source_path=f"sessions/{session_id}/commands/000001.json",
            session_id=session_id,
            sequence=1,
            document_commit_sha="b" * 40,
            raw_content=cmd1,
            raw_sha256_value="c" * 64,
        )

        with journal._transaction():
            journal._connection.execute(
                "INSERT INTO sessions (session_id, repository_id, base_sha, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, "origin", "a" * 40, SessionState.CREATED.value, fixed_now(), fixed_now())
            )
            journal._connection.execute(
                "INSERT INTO commands (command_id, session_id, sequence, command_sha256, command_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{session_id}:000001", session_id, 1, "c" * 64, "{}", CommandState.DISCOVERED.value, fixed_now(), fixed_now())
            )

        manifest = manifest_payload(session_id)
        def trigger_collision():
            with pytest.raises(CollisionError):
                journal.record_session_manifest(
                    source_id="commands",
                    snapshot_sha="a" * 40,
                    source_path=f"sessions/{session_id}/manifest.json",
                    session_id=session_id,
                    manifest_commit_sha="b" * 40,
                    raw_content=json.dumps(manifest),
                    manifest_json=json.dumps(manifest),
                    manifest_sha256=sha256_text(json.dumps(manifest)),
                    raw_sha256="c" * 64,
                    repository_id="origin",
                    base_sha="a" * 40,
                    created_remote_at=fixed_now(),
                    expires_at="2026-07-15T09:00:00Z",
                )

    trigger_collision()

    issues = get_ingestion_issues(journal)
    assert len(issues) == 1
    events = [e for e in journal.list_events() if e.event_type == "ingestion.blocked"]
    assert len(events) == 1

    journal.close()
    journal = Journal.open(db_path, now_fn=fixed_now)

    issues = get_ingestion_issues(journal)
    assert len(issues) == 1
    events = [e for e in journal.list_events() if e.event_type == "ingestion.blocked"]
    assert len(events) == 1

    for _ in range(10):
        trigger_collision()

    issues = get_ingestion_issues(journal)
    assert len(issues) == 1
    events = [e for e in journal.list_events() if e.event_type == "ingestion.blocked"]
    assert len(events) == 1

    journal.close()


def test_invalid_utf8_ingestion_semantics(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.db"
    journal = Journal.open(db_path, now_fn=fixed_now)

    session_id = SESSION_ID
    invalid_bytes = b"\xff\xfeinvalid_utf8"

    # 1. staged command with invalid UTF-8 before manifest
    journal.record_ingested_command(
        source_id="commands",
        snapshot_sha="e" * 40,
        source_path=f"sessions/{session_id}/commands/000001.json",
        session_id=session_id,
        sequence=1,
        document_commit_sha="0000000000000000000000000000000000000001",
        raw_content=invalid_bytes,
        raw_sha256_value=calculate_raw_sha256(invalid_bytes),
    )

    # 2. reopen Journal
    journal.close()
    journal = Journal.open(db_path, now_fn=fixed_now)

    # 3. manifest appearance
    snapshot_manifest = make_snapshot(session_id=session_id, sequences=())
    ingestor = CommandIngestor(journal, FakeTransport(snapshot_manifest))
    report = ingestor.poll_once()
    assert report.error_code is None

    # 4. staged row still exists
    staged = journal._connection.execute(
        "SELECT 1 FROM pending_command_documents WHERE session_id = ? AND sequence = 1",
        (session_id,)
    ).fetchone()
    assert staged is not None

    # 5. no commands row
    cmd = journal.get_command(f"{session_id}:000001")
    assert cmd is None

    # 6. exactly one non-blocking issue
    issues = get_ingestion_issues(journal)
    assert len(issues) == 1
    assert issues[0].error_code == BridgeErrorCode.INVALID_PAYLOAD.value
    assert issues[0].blocking is False

    # 7. no ingestion.blocked event
    events = [e for e in journal.list_events() if e.event_type == "ingestion.blocked"]
    assert len(events) == 0

    # 8. attempt_count == 0
    src = journal.get_ingestion_source("commands")
    assert src.attempt_count == 0

    # 9. poll_once does not return INGESTION_BLOCKED
    report2 = ingestor.poll_once()
    assert report2.error_code is None

    # 10. valid command in another session is claimable
    other_session = "018f3f66-6cb3-4f66-9f2e-3d7647d1b702"
    snapshot_other = make_snapshot(session_id=other_session, sequences=(1,))
    ingestor_other = CommandIngestor(journal, FakeTransport(snapshot_other))
    report_other = ingestor_other.poll_once()
    assert report_other.error_code is None

    ingestor_other.validate_pending()

    claimed = journal.claim_next_command()
    assert claimed is not None
    assert claimed.session_id == other_session

    # repeat poll 10 times
    for _ in range(10):
        ingestor.poll_once()

    issues = get_ingestion_issues(journal)
    assert len(issues) == 1
    events = [e for e in journal.list_events() if e.event_type == "ingestion.blocked"]
    assert len(events) == 0
    src = journal.get_ingestion_source("commands")
    assert src.attempt_count == 0

    # invalid UTF-8 post manifest
    third_session = "018f3f66-6cb3-4f66-9f2e-3d7647d1b703"
    snapshot_third = make_snapshot(session_id=third_session, sequences=())
    ingestor_third = CommandIngestor(journal, FakeTransport(snapshot_third))
    report_third = ingestor_third.poll_once()
    assert report_third.error_code is None

    journal.record_ingested_command(
        source_id="commands",
        snapshot_sha=snapshot_third.snapshot_sha,
        source_path=f"sessions/{third_session}/commands/000001.json",
        session_id=third_session,
        sequence=1,
        document_commit_sha="b" * 40,
        raw_content=invalid_bytes,
        raw_sha256_value=calculate_raw_sha256(invalid_bytes),
    )

    issues_all = get_ingestion_issues(journal)
    assert len(issues_all) == 2

    issue_third = [i for i in issues_all if i.session_id == third_session][0]
    assert issue_third.error_code == BridgeErrorCode.INVALID_PAYLOAD.value
    assert issue_third.blocking is False

    journal.close()
