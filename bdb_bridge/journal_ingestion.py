from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .ingestion_validate import is_expired
from .models import (
    BridgeErrorCode,
    CommandIngestionRecord,
    CommandRecord,
    CommandState,
    IngestionIssue,
    SCHEDULER_PREDECESSOR_BLOCKING_STATES,
    SCHEDULER_PREDECESSOR_DONE_STATES,
    SessionIngestionRecord,
    SessionState,
    TransportRetryRecord,
    validate_command_transition,
    validate_session_transition,
)
from .protocol import BridgeError, command_id_for, parse_strict_utc_timestamp
from .serializers import canonical_json, sha256_text

if TYPE_CHECKING:
    from .journal import Journal

MAX_LAST_ERROR_LEN = 512
DEFAULT_SOURCE_ID = "commands"


def compute_backoff_delay(attempt_count: int, *, base_delay: float = 1.0, max_delay: float = 60.0) -> float:
    if attempt_count <= 0:
        return 0.0
    return min(base_delay * (2 ** (attempt_count - 1)), max_delay)


def sanitize_transport_error(message: str) -> str:
    cleaned = " ".join(message.split())
    if len(cleaned) > MAX_LAST_ERROR_LEN:
        return cleaned[: MAX_LAST_ERROR_LEN - 3] + "..."
    return cleaned


def _row_to_transport_retry(row: tuple[Any, ...]) -> TransportRetryRecord:
    return TransportRetryRecord(
        source_id=row[0],
        last_observed_sha=row[1],
        attempt_count=int(row[2]),
        next_attempt_at=row[3],
        last_error=row[4],
        last_success_at=row[5],
        updated_at=row[6],
    )


def _row_to_session_ingestion(row: tuple[Any, ...]) -> SessionIngestionRecord:
    return SessionIngestionRecord(
        session_id=row[0],
        source_path=row[1],
        manifest_commit_sha=row[2],
        raw_sha256=row[3],
        manifest_sha256=row[4],
        manifest_json=row[5],
        created_remote_at=row[6],
        expires_at=row[7],
        first_seen_at=row[8],
        last_seen_at=row[9],
    )


def _row_to_command_ingestion(row: tuple[Any, ...]) -> CommandIngestionRecord:
    return CommandIngestionRecord(
        command_id=row[0],
        source_path=row[1],
        document_commit_sha=row[2],
        raw_sha256=row[3],
        created_remote_at=row[4],
        expires_at=row[5],
        first_seen_at=row[6],
        last_seen_at=row[7],
    )


def _row_to_ingestion_issue(row: tuple[Any, ...]) -> IngestionIssue:
    return IngestionIssue(
        issue_id=int(row[0]),
        source_id=row[1],
        source_path=row[2],
        snapshot_sha=row[3],
        document_commit_sha=row[4],
        raw_sha256=row[5],
        session_id=row[6],
        command_id=row[7],
        error_code=row[8],
        detail=row[9],
        blocking=bool(row[10]),
        created_at=row[11],
    )


def get_ingestion_source(journal: Journal, source_id: str) -> TransportRetryRecord:
    journal._ensure_open()
    row = journal._connection.execute(
        """
        SELECT source_id, last_observed_sha, attempt_count, next_attempt_at,
               last_error, last_success_at, updated_at
        FROM ingestion_sources WHERE source_id = ?
        """,
        (source_id,),
    ).fetchone()
    if row is None:
        now = journal._now_fn()
        return TransportRetryRecord(
            source_id=source_id,
            last_observed_sha=None,
            attempt_count=0,
            next_attempt_at=None,
            last_error=None,
            last_success_at=None,
            updated_at=now,
        )
    return _row_to_transport_retry(row)


def record_transport_failure(
    journal: Journal,
    source_id: str,
    error_message: str,
    *,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> TransportRetryRecord:
    journal._ensure_open()
    sanitized = sanitize_transport_error(error_message)
    now = journal._now_fn()
    with journal._transaction():
        row = journal._connection.execute(
            "SELECT attempt_count FROM ingestion_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        attempt_count = int(row[0]) + 1 if row is not None else 1
        delay = compute_backoff_delay(attempt_count, base_delay=base_delay, max_delay=max_delay)
        next_attempt_at = _add_seconds_iso(now, delay)
        if row is None:
            journal._connection.execute(
                """
                INSERT INTO ingestion_sources (
                    source_id, last_observed_sha, attempt_count, next_attempt_at,
                    last_error, last_success_at, updated_at
                ) VALUES (?, NULL, ?, ?, ?, NULL, ?)
                """,
                (source_id, attempt_count, next_attempt_at, sanitized, now),
            )
        else:
            journal._connection.execute(
                """
                UPDATE ingestion_sources
                SET attempt_count = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (attempt_count, next_attempt_at, sanitized, now, source_id),
            )
        journal._append_event_in_transaction(
            event_type="transport.fetch_failed",
            payload={"source_id": source_id, "attempt_count": attempt_count},
            created_at=now,
        )
    return get_ingestion_source(journal, source_id)


def record_transport_success(
    journal: Journal,
    source_id: str,
    snapshot_sha: str,
) -> TransportRetryRecord:
    journal._ensure_open()
    now = journal._now_fn()
    with journal._transaction():
        row = journal._connection.execute(
            "SELECT source_id FROM ingestion_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            journal._connection.execute(
                """
                INSERT INTO ingestion_sources (
                    source_id, last_observed_sha, attempt_count, next_attempt_at,
                    last_error, last_success_at, updated_at
                ) VALUES (?, ?, 0, NULL, NULL, ?, ?)
                """,
                (source_id, snapshot_sha, now, now),
            )
        else:
            journal._connection.execute(
                """
                UPDATE ingestion_sources
                SET last_observed_sha = ?, attempt_count = 0, next_attempt_at = NULL,
                    last_error = NULL, last_success_at = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (snapshot_sha, now, now, source_id),
            )
        journal._append_event_in_transaction(
            event_type="transport.fetch_succeeded",
            payload={"source_id": source_id, "snapshot_sha": snapshot_sha},
            created_at=now,
        )
    return get_ingestion_source(journal, source_id)


def get_session_ingestion(journal: Journal, session_id: str) -> SessionIngestionRecord | None:
    journal._ensure_open()
    row = journal._connection.execute(
        """
        SELECT session_id, source_path, manifest_commit_sha, raw_sha256, manifest_sha256,
               manifest_json, created_remote_at, expires_at, first_seen_at, last_seen_at
        FROM session_ingestion WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_session_ingestion(row)


def get_command_ingestion(journal: Journal, command_id: str) -> CommandIngestionRecord | None:
    journal._ensure_open()
    row = journal._connection.execute(
        """
        SELECT command_id, source_path, document_commit_sha, raw_sha256,
               created_remote_at, expires_at, first_seen_at, last_seen_at
        FROM command_ingestion WHERE command_id = ?
        """,
        (command_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_command_ingestion(row)


def has_blocking_ingestion_issues(journal: Journal) -> bool:
    journal._ensure_open()
    row = journal._connection.execute(
        "SELECT 1 FROM ingestion_issues WHERE blocking = 1 LIMIT 1"
    ).fetchone()
    return row is not None


def record_ingestion_issue(
    journal: Journal,
    *,
    source_id: str,
    source_path: str,
    snapshot_sha: str,
    raw_sha256: str,
    error_code: str,
    detail: str,
    blocking: bool,
    document_commit_sha: str | None = None,
    session_id: str | None = None,
    command_id: str | None = None,
) -> IngestionIssue | None:
    journal._ensure_open()
    now = journal._now_fn()
    with journal._transaction():
        existing = journal._connection.execute(
            """
            SELECT issue_id, source_id, source_path, snapshot_sha, document_commit_sha,
                   raw_sha256, session_id, command_id, error_code, detail, blocking, created_at
            FROM ingestion_issues
            WHERE source_id = ? AND source_path = ? AND error_code = ? AND raw_sha256 = ?
            """,
            (source_id, source_path, error_code, raw_sha256),
        ).fetchone()
        if existing is not None:
            return _row_to_ingestion_issue(existing)
        cursor = journal._connection.execute(
            """
            INSERT INTO ingestion_issues (
                source_id, source_path, snapshot_sha, document_commit_sha, raw_sha256,
                session_id, command_id, error_code, detail, blocking, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                source_path,
                snapshot_sha,
                document_commit_sha,
                raw_sha256,
                session_id,
                command_id,
                error_code,
                detail,
                1 if blocking else 0,
                now,
            ),
        )
        issue_id = int(cursor.lastrowid)
        if blocking:
            journal._append_event_in_transaction(
                session_id=session_id,
                command_id=command_id,
                event_type="ingestion.blocked",
                payload={"source_path": source_path, "error_code": error_code},
                created_at=now,
            )
    row = journal._connection.execute(
        """
        SELECT issue_id, source_id, source_path, snapshot_sha, document_commit_sha,
               raw_sha256, session_id, command_id, error_code, detail, blocking, created_at
        FROM ingestion_issues WHERE issue_id = ?
        """,
        (issue_id,),
    ).fetchone()
    assert row is not None
    return _row_to_ingestion_issue(row)


def record_session_manifest(
    journal: Journal,
    *,
    source_id: str,
    snapshot_sha: str,
    source_path: str,
    session_id: str,
    manifest_commit_sha: str,
    raw_content: str,
    manifest_json: str,
    manifest_sha256: str,
    raw_sha256: str,
    repository_id: str,
    base_sha: str,
    created_remote_at: str,
    expires_at: str,
) -> SessionIngestionRecord:
    journal._ensure_open()
    now = journal._now_fn()
    existing = journal._connection.execute(
        """
        SELECT session_id, source_path, manifest_commit_sha, raw_sha256, manifest_sha256,
               manifest_json, created_remote_at, expires_at, first_seen_at, last_seen_at
        FROM session_ingestion WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing[1] == source_path
            and existing[2] == manifest_commit_sha
            and existing[3] == raw_sha256
            and existing[4] == manifest_sha256
            and existing[5] == manifest_json
        ):
            with journal._transaction():
                journal._connection.execute(
                    "UPDATE session_ingestion SET last_seen_at = ? WHERE session_id = ?",
                    (now, session_id),
                )
            return _row_to_session_ingestion(
                (
                    existing[0],
                    existing[1],
                    existing[2],
                    existing[3],
                    existing[4],
                    existing[5],
                    existing[6],
                    existing[7],
                    existing[8],
                    now,
                )
            )
        with journal._transaction():
            record_ingestion_issue_in_transaction(
                journal,
                source_id=source_id,
                source_path=source_path,
                snapshot_sha=snapshot_sha,
                raw_sha256=raw_sha256,
                error_code=BridgeErrorCode.SESSION_ID_COLLISION.value,
                detail=f"Manifest collision for session {session_id}",
                blocking=True,
                document_commit_sha=manifest_commit_sha,
                session_id=session_id,
                created_at=now,
            )
        raise BridgeError(
            BridgeErrorCode.SESSION_ID_COLLISION,
            f"Manifest collision for session {session_id}",
        )

    with journal._transaction():
        session_row = journal._connection.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session_row is None:
            journal._connection.execute(
                """
                INSERT INTO sessions (
                    session_id, repository_id, base_sha, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, repository_id, base_sha, SessionState.CREATED.value, now, now),
            )
            journal._append_event_in_transaction(
                session_id=session_id,
                event_type="session.created",
                payload={"state": SessionState.CREATED.value},
                created_at=now,
            )

        journal._connection.execute(
            """
            INSERT INTO session_ingestion (
                session_id, source_path, manifest_commit_sha, raw_sha256, manifest_sha256,
                manifest_json, created_remote_at, expires_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                source_path,
                manifest_commit_sha,
                raw_sha256,
                manifest_sha256,
                manifest_json,
                created_remote_at,
                expires_at,
                now,
                now,
            ),
        )
        journal._append_event_in_transaction(
            session_id=session_id,
            event_type="session.manifest_recorded",
            payload={"source_path": source_path, "manifest_commit_sha": manifest_commit_sha},
            created_at=now,
        )
        _promote_pending_commands_in_transaction(journal, session_id, now)

    record = get_session_ingestion(journal, session_id)
    assert record is not None
    return record


def record_ingested_command(
    journal: Journal,
    *,
    source_id: str,
    snapshot_sha: str,
    source_path: str,
    session_id: str,
    sequence: int,
    document_commit_sha: str,
    raw_content: str,
    raw_sha256_value: str,
) -> CommandIngestionRecord | None:
    journal._ensure_open()
    command_id = command_id_for(session_id, sequence)
    now = journal._now_fn()

    session_row = journal.get_session(session_id)
    if session_row is None:
        return None

    command_sha256 = raw_sha256_value
    expected_revision: int | None = None
    expected_state_hash: str | None = None
    try:
        parsed = json.loads(raw_content)
        if isinstance(parsed, dict):
            command_sha256 = sha256_text(canonical_json(parsed))
            if "expected_revision" in parsed and isinstance(parsed["expected_revision"], int):
                expected_revision = parsed["expected_revision"]
            if "expected_state_hash" in parsed:
                value = parsed["expected_state_hash"]
                expected_state_hash = value if isinstance(value, str) and value else None
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    with journal._transaction():
        existing_ingestion = journal._connection.execute(
            """
            SELECT command_id, source_path, document_commit_sha, raw_sha256,
                   created_remote_at, expires_at, first_seen_at, last_seen_at
            FROM command_ingestion WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()

        existing_command = journal._get_command_row_for_update(command_id)

        if existing_ingestion is not None:
            if (
                existing_ingestion[1] == source_path
                and existing_ingestion[2] == document_commit_sha
                and existing_ingestion[3] == raw_sha256_value
            ):
                journal._connection.execute(
                    "UPDATE command_ingestion SET last_seen_at = ? WHERE command_id = ?",
                    (now, command_id),
                )
                return _row_to_command_ingestion(
                    (
                        existing_ingestion[0],
                        existing_ingestion[1],
                        existing_ingestion[2],
                        existing_ingestion[3],
                        existing_ingestion[4],
                        existing_ingestion[5],
                        existing_ingestion[6],
                        now,
                    )
                )
            record_ingestion_issue_in_transaction(
                journal,
                source_id=source_id,
                source_path=source_path,
                snapshot_sha=snapshot_sha,
                raw_sha256=raw_sha256_value,
                error_code=BridgeErrorCode.COMMAND_ID_COLLISION.value,
                detail=f"Command document collision for {command_id}",
                blocking=True,
                document_commit_sha=document_commit_sha,
                session_id=session_id,
                command_id=command_id,
                created_at=now,
            )
            raise BridgeError(
                BridgeErrorCode.COMMAND_ID_COLLISION,
                f"Command document collision for {command_id}",
            )

        if existing_command is not None:
            if (
                existing_command[1] == session_id
                and existing_command[2] == sequence
                and existing_command[4] == raw_content
                and existing_command[5] == document_commit_sha
            ):
                journal._connection.execute(
                    """
                    INSERT INTO command_ingestion (
                        command_id, source_path, document_commit_sha, raw_sha256,
                        created_remote_at, expires_at, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (command_id, source_path, document_commit_sha, raw_sha256_value, now, now),
                )
                return get_command_ingestion(journal, command_id)
            record_ingestion_issue_in_transaction(
                journal,
                source_id=source_id,
                source_path=source_path,
                snapshot_sha=snapshot_sha,
                raw_sha256=raw_sha256_value,
                error_code=BridgeErrorCode.SEQUENCE_COLLISION.value,
                detail=f"Sequence collision for session {session_id} sequence {sequence}",
                blocking=True,
                document_commit_sha=document_commit_sha,
                session_id=session_id,
                command_id=command_id,
                created_at=now,
            )
            raise BridgeError(
                BridgeErrorCode.SEQUENCE_COLLISION,
                f"Sequence collision for session {session_id} sequence {sequence}",
            )

        seq_row = journal._connection.execute(
            "SELECT command_id FROM commands WHERE session_id = ? AND sequence = ?",
            (session_id, sequence),
        ).fetchone()
        if seq_row is not None and seq_row[0] != command_id:
            record_ingestion_issue_in_transaction(
                journal,
                source_id=source_id,
                source_path=source_path,
                snapshot_sha=snapshot_sha,
                raw_sha256=raw_sha256_value,
                error_code=BridgeErrorCode.SEQUENCE_COLLISION.value,
                detail=f"Sequence collision for session {session_id} sequence {sequence}",
                blocking=True,
                document_commit_sha=document_commit_sha,
                session_id=session_id,
                command_id=command_id,
                created_at=now,
            )
            raise BridgeError(
                BridgeErrorCode.SEQUENCE_COLLISION,
                f"Sequence collision for session {session_id} sequence {sequence}",
            )

        journal._connection.execute(
            """
            INSERT INTO commands (
                command_id, session_id, sequence, command_sha256, command_json,
                command_commit_sha, state, expected_revision, expected_state_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                session_id,
                sequence,
                command_sha256,
                raw_content,
                document_commit_sha,
                CommandState.DISCOVERED.value,
                expected_revision,
                expected_state_hash,
                now,
                now,
            ),
        )
        journal._connection.execute(
            """
            INSERT INTO command_ingestion (
                command_id, source_path, document_commit_sha, raw_sha256,
                created_remote_at, expires_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (command_id, source_path, document_commit_sha, raw_sha256_value, now, now),
        )
        journal._append_event_in_transaction(
            session_id=session_id,
            command_id=command_id,
            event_type="command.discovered",
            payload={"sequence": sequence, "source_path": source_path},
            created_at=now,
        )

    return get_command_ingestion(journal, command_id)


def list_discovered_commands(journal: Journal) -> list[CommandRecord]:
    journal._ensure_open()
    rows = journal._connection.execute(
        """
        SELECT command_id, session_id, sequence, command_sha256, command_json,
               command_commit_sha, state, expected_revision, expected_state_hash,
               created_at, updated_at
        FROM commands
        WHERE state = ?
        ORDER BY session_id ASC, sequence ASC
        """,
        (CommandState.DISCOVERED.value,),
    ).fetchall()
    from .journal import _row_to_command

    return [_row_to_command(row) for row in rows]


def claim_next_command(journal: Journal) -> CommandRecord | None:
    journal._ensure_open()
    now = journal._now_fn()
    now_dt = parse_strict_utc_timestamp(now, field="now")

    with journal._transaction():
        if has_blocking_ingestion_issues_in_transaction(journal):
            return None

        worker = journal._connection.execute(
            """
            SELECT command_id FROM commands
            WHERE state IN (?, ?, ?)
            LIMIT 1
            """,
            (
                CommandState.CLAIMED.value,
                CommandState.EXECUTING.value,
                CommandState.EFFECT_RECORDED.value,
            ),
        ).fetchone()
        if worker is not None:
            return None

        active_session = journal._connection.execute(
            """
            SELECT session_id FROM sessions
            WHERE state IN (?, ?)
            ORDER BY created_at ASC, session_id ASC
            LIMIT 1
            """,
            (SessionState.ACTIVE.value, SessionState.COMPLETING.value),
        ).fetchone()

        target_session_id: str | None
        if active_session is not None:
            target_session_id = active_session[0]
        else:
            candidate = journal._connection.execute(
                """
                SELECT s.session_id
                FROM sessions s
                JOIN commands c ON c.session_id = s.session_id AND c.sequence = 1
                WHERE s.state = ? AND c.state = ?
                ORDER BY s.created_at ASC, s.session_id ASC
                LIMIT 1
                """,
                (SessionState.CREATED.value, CommandState.VALIDATED.value),
            ).fetchone()
            if candidate is None:
                return None
            target_session_id = candidate[0]

        manifest = journal._connection.execute(
            "SELECT expires_at FROM session_ingestion WHERE session_id = ?",
            (target_session_id,),
        ).fetchone()
        if manifest is not None and is_expired(manifest[0], now=now_dt):
            return None

        command_row = journal._connection.execute(
            """
            SELECT command_id, session_id, sequence, command_sha256, command_json,
                   command_commit_sha, state, expected_revision, expected_state_hash,
                   created_at, updated_at
            FROM commands
            WHERE session_id = ? AND state = ?
            ORDER BY sequence ASC
            LIMIT 1
            """,
            (target_session_id, CommandState.VALIDATED.value),
        ).fetchone()
        if command_row is None:
            return None

        sequence = int(command_row[2])
        if sequence > 1:
            predecessor = journal._connection.execute(
                """
                SELECT state FROM commands
                WHERE session_id = ? AND sequence = ?
                """,
                (target_session_id, sequence - 1),
            ).fetchone()
            if predecessor is None:
                return None
            pred_state = CommandState(predecessor[0])
            if pred_state in SCHEDULER_PREDECESSOR_BLOCKING_STATES:
                return None
            if pred_state not in SCHEDULER_PREDECESSOR_DONE_STATES:
                return None

        session_state_row = journal._connection.execute(
            "SELECT state FROM sessions WHERE session_id = ?",
            (target_session_id,),
        ).fetchone()
        assert session_state_row is not None
        session_state = SessionState(session_state_row[0])

        if session_state is SessionState.CREATED:
            validate_session_transition(session_state, SessionState.ACTIVE)
            updated_session = journal._connection.execute(
                """
                UPDATE sessions SET state = ?, updated_at = ?
                WHERE session_id = ? AND state = ?
                """,
                (SessionState.ACTIVE.value, now, target_session_id, SessionState.CREATED.value),
            )
            if updated_session.rowcount != 1:
                return None
            journal._append_event_in_transaction(
                session_id=target_session_id,
                event_type="session.activated",
                payload={"state": SessionState.ACTIVE.value},
                created_at=now,
            )

        command_id = command_row[0]
        validate_command_transition(CommandState.VALIDATED, CommandState.CLAIMED)
        updated_command = journal._connection.execute(
            """
            UPDATE commands SET state = ?, updated_at = ?
            WHERE command_id = ? AND state = ?
            """,
            (CommandState.CLAIMED.value, now, command_id, CommandState.VALIDATED.value),
        )
        if updated_command.rowcount != 1:
            return None
        journal._append_event_in_transaction(
            session_id=target_session_id,
            command_id=command_id,
            event_type="command.claimed",
            payload={"sequence": sequence},
            created_at=now,
        )
        journal._append_event_in_transaction(
            session_id=target_session_id,
            command_id=command_id,
            event_type="command.state_changed",
            payload={
                "from_state": CommandState.VALIDATED.value,
                "to_state": CommandState.CLAIMED.value,
            },
            created_at=now,
        )
        claimed_row = (
            command_id,
            command_row[1],
            command_row[2],
            command_row[3],
            command_row[4],
            command_row[5],
            CommandState.CLAIMED.value,
            command_row[7],
            command_row[8],
            command_row[9],
            now,
        )

    from .journal import _row_to_command

    return _row_to_command(claimed_row)


def record_ingestion_issue_in_transaction(
    journal: Journal,
    *,
    source_id: str,
    source_path: str,
    snapshot_sha: str,
    raw_sha256: str,
    error_code: str,
    detail: str,
    blocking: bool,
    created_at: str,
    document_commit_sha: str | None = None,
    session_id: str | None = None,
    command_id: str | None = None,
) -> None:
    existing = journal._connection.execute(
        """
        SELECT 1 FROM ingestion_issues
        WHERE source_id = ? AND source_path = ? AND error_code = ? AND raw_sha256 = ?
        """,
        (source_id, source_path, error_code, raw_sha256),
    ).fetchone()
    if existing is not None:
        return
    journal._connection.execute(
        """
        INSERT INTO ingestion_issues (
            source_id, source_path, snapshot_sha, document_commit_sha, raw_sha256,
            session_id, command_id, error_code, detail, blocking, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            source_path,
            snapshot_sha,
            document_commit_sha,
            raw_sha256,
            session_id,
            command_id,
            error_code,
            detail,
            1 if blocking else 0,
            created_at,
        ),
    )
    if blocking:
        journal._append_event_in_transaction(
            session_id=session_id,
            command_id=command_id,
            event_type="ingestion.blocked",
            payload={"source_path": source_path, "error_code": error_code},
            created_at=created_at,
        )


def has_blocking_ingestion_issues_in_transaction(journal: Journal) -> bool:
    row = journal._connection.execute(
        "SELECT 1 FROM ingestion_issues WHERE blocking = 1 LIMIT 1"
    ).fetchone()
    return row is not None


def list_session_ingestions(journal: Journal) -> list[SessionIngestionRecord]:
    journal._ensure_open()
    rows = journal._connection.execute(
        """
        SELECT session_id, source_path, manifest_commit_sha, raw_sha256, manifest_sha256,
               manifest_json, created_remote_at, expires_at, first_seen_at, last_seen_at
        FROM session_ingestion
        ORDER BY session_id ASC
        """
    ).fetchall()
    return [_row_to_session_ingestion(row) for row in rows]


def update_command_canonical(
    journal: Journal,
    command_id: str,
    *,
    command_json: str,
    command_sha256: str,
) -> None:
    journal._ensure_open()
    now = journal._now_fn()
    with journal._transaction():
        journal._connection.execute(
            """
            UPDATE commands
            SET command_json = ?, command_sha256 = ?, updated_at = ?
            WHERE command_id = ?
            """,
            (command_json, command_sha256, now, command_id),
        )


def transition_command_semantic(
    journal: Journal,
    command_id: str,
    expected_state: CommandState,
    new_state: CommandState,
    *,
    semantic_event_type: str,
) -> CommandRecord:
    journal._ensure_open()
    now = journal._now_fn()
    with journal._transaction():
        row = journal._get_command_row_for_update(command_id)
        if row is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Command not found: {command_id}",
            )
        current_state = CommandState(row[6])
        if current_state != expected_state:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Command state mismatch: expected {expected_state.value}, got {current_state.value}",
            )
        validate_command_transition(current_state, new_state)
        updated = journal._connection.execute(
            """
            UPDATE commands SET state = ?, updated_at = ?
            WHERE command_id = ? AND state = ?
            """,
            (new_state.value, now, command_id, expected_state.value),
        )
        if updated.rowcount != 1:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Failed to transition command {command_id}",
            )
        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type=semantic_event_type,
            payload={"sequence": row[2], "state": new_state.value},
            created_at=now,
        )
        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type="command.state_changed",
            payload={"from_state": expected_state.value, "to_state": new_state.value},
            created_at=now,
        )
        claimed_row = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            new_state.value,
            row[7],
            row[8],
            row[9],
            now,
        )
    from .journal import _row_to_command

    return _row_to_command(claimed_row)


def expire_stale_sessions(journal: Journal, *, now_dt: datetime) -> None:
    for record in list_session_ingestions(journal):
        if not is_expired(record.expires_at, now=now_dt):
            continue
        session = journal.get_session(record.session_id)
        if session is None or session.state is not SessionState.CREATED:
            continue
        journal.transition_session(record.session_id, SessionState.CREATED, SessionState.ABORTED)
        discovered = [
            command
            for command in list_discovered_commands(journal)
            if command.session_id == record.session_id
        ]
        for command in discovered:
            transition_command_semantic(
                journal,
                command.command_id,
                CommandState.DISCOVERED,
                CommandState.EXPIRED,
                semantic_event_type="command.expired",
            )


def _promote_pending_commands_in_transaction(journal: Journal, session_id: str, now: str) -> None:
    del journal, session_id, now


def _add_seconds_iso(iso_timestamp: str, seconds: float) -> str:
    parsed = parse_strict_utc_timestamp(iso_timestamp, field="timestamp")
    from datetime import timedelta

    updated = parsed + timedelta(seconds=seconds)
    return updated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
