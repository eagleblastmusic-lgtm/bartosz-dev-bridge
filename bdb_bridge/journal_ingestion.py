from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
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
    PromotionOutcome,
)
from .protocol import BridgeError, command_id_for, parse_strict_utc_timestamp
from .serializers import canonical_json, sha256_text

if TYPE_CHECKING:
    from .journal import Journal

MAX_LAST_ERROR_LEN = 512
DEFAULT_SOURCE_ID = "commands"


class CollisionError(BridgeError):
    def __init__(self, code: BridgeErrorCode, message: str, issue_created: bool, report: Any = None, promotion_outcome: Any = None) -> None:
        super().__init__(code, message)
        self.issue_created = issue_created
        self.report = report
        self.promotion_outcome = promotion_outcome


def is_hex(s: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in s)


def validate_sha40(name: str, val: str) -> None:
    if not isinstance(val, str) or len(val) != 40 or not is_hex(val):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{name} must be a 40-character hex string, got {val!r}",
        )


def validate_sha64(name: str, val: str) -> None:
    if not isinstance(val, str):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{name} must be a string",
        )
    actual = val
    if val.startswith("sha256:"):
        actual = val[7:]
    if len(actual) != 64 or not is_hex(actual):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{name} must be a 64-character hex string (optionally prefixed with sha256:), got {val!r}",
        )


def compute_backoff_delay(attempt_count: int, *, base_delay: float = 1.0, max_delay: float = 60.0) -> float:
    if attempt_count <= 0:
        return 0.0
    if base_delay <= 0.0:
        base_delay = 1.0
    if max_delay <= 0.0:
        max_delay = 60.0
    if max_delay < base_delay:
        max_delay = base_delay
    if attempt_count > 100:
        return max_delay
    return min(base_delay * (2 ** (attempt_count - 1)), max_delay)


def sanitize_transport_error(message: str) -> str:
    cleaned = " ".join(message.split())
    if len(cleaned) > MAX_LAST_ERROR_LEN:
        return cleaned[: MAX_LAST_ERROR_LEN - 3] + "..."
    return cleaned


def sanitize_detail(detail: str) -> str:
    cleaned = " ".join(detail.split())
    if len(cleaned) > 1024:
        return cleaned[:1021] + "..."
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
        source_id=row[1],
        snapshot_sha=row[2],
        source_path=row[3],
        document_commit_sha=row[4],
        raw_sha256=row[5],
        created_remote_at=row[6],
        expires_at=row[7],
        first_seen_at=row[8],
        last_seen_at=row[9],
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
    validate_sha40("snapshot_sha", snapshot_sha)
    now = journal._now_fn()
    with journal._transaction():
        row = journal._connection.execute(
            "SELECT last_observed_sha, attempt_count, last_error FROM ingestion_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()

        should_emit_event = False
        if row is None:
            should_emit_event = True
        else:
            last_observed_sha, attempt_count, last_error = row
            if last_observed_sha != snapshot_sha:
                should_emit_event = True
            elif attempt_count > 0 or last_error is not None:
                should_emit_event = True

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

        if should_emit_event:
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
        SELECT command_id, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256,
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
) -> tuple[IngestionIssue, bool] | None:
    journal._ensure_open()
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty string")
    validate_sha40("snapshot_sha", snapshot_sha)
    if document_commit_sha is not None:
        validate_sha40("document_commit_sha", document_commit_sha)
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("source_path must be a non-empty string")
    validate_sha64("raw_sha256", raw_sha256)
    if not isinstance(blocking, bool):
        raise ValueError("blocking must be a boolean")

    detail = sanitize_detail(detail)
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
            return _row_to_ingestion_issue(existing), False

        created = record_ingestion_issue_in_transaction(
            journal,
            source_id=source_id,
            source_path=source_path,
            snapshot_sha=snapshot_sha,
            raw_sha256=raw_sha256,
            error_code=error_code,
            detail=detail,
            blocking=blocking,
            created_at=now,
            document_commit_sha=document_commit_sha,
            session_id=session_id,
            command_id=command_id,
        )

        row = journal._connection.execute(
            """
            SELECT issue_id, source_id, source_path, snapshot_sha, document_commit_sha,
                   raw_sha256, session_id, command_id, error_code, detail, blocking, created_at
            FROM ingestion_issues
            WHERE source_id = ? AND source_path = ? AND error_code = ? AND raw_sha256 = ?
            """,
            (source_id, source_path, error_code, raw_sha256),
        ).fetchone()
        assert row is not None
        return _row_to_ingestion_issue(row), created


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
) -> bool:
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty string")
    validate_sha40("snapshot_sha", snapshot_sha)
    if document_commit_sha is not None:
        validate_sha40("document_commit_sha", document_commit_sha)
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("source_path must be a non-empty string")
    validate_sha64("raw_sha256", raw_sha256)
    if not isinstance(blocking, bool):
        raise ValueError("blocking must be a boolean")

    detail = sanitize_detail(detail)
    existing = journal._connection.execute(
        """
        SELECT 1 FROM ingestion_issues
        WHERE source_id = ? AND source_path = ? AND error_code = ? AND raw_sha256 = ?
        """,
        (source_id, source_path, error_code, raw_sha256),
    ).fetchone()
    if existing is not None:
        return False
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
    return True


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
) -> tuple[SessionIngestionRecord, bool, PromotionOutcome]:
    journal._ensure_open()
    validate_sha40("snapshot_sha", snapshot_sha)
    validate_sha40("manifest_commit_sha", manifest_commit_sha)
    validate_sha64("manifest_sha256", manifest_sha256)
    validate_sha64("raw_sha256", raw_sha256)
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
            has_pending = journal._connection.execute(
                "SELECT 1 FROM pending_command_documents WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone() is not None

            if not has_pending:
                with journal._transaction():
                    journal._connection.execute(
                        "UPDATE session_ingestion SET last_seen_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                rec = _row_to_session_ingestion(
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
                return rec, False, PromotionOutcome(0, 0, ())

            with journal._transaction():
                journal._connection.execute(
                    "UPDATE session_ingestion SET last_seen_at = ? WHERE session_id = ?",
                    (now, session_id),
                )
                outcome = _promote_pending_commands_in_transaction(journal, session_id, now)

            rec = _row_to_session_ingestion(
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

            if outcome.blocking_collisions:
                err = outcome.blocking_collisions[0]
                err.promotion_outcome = outcome
                raise err

            return rec, False, outcome

        with journal._transaction():
            issue_created = record_ingestion_issue_in_transaction(
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
        raise CollisionError(
            BridgeErrorCode.SESSION_ID_COLLISION,
            f"Manifest collision for session {session_id}",
            issue_created=issue_created,
        )

    # Check for session collision with existing v1 database session row
    session_row = None
    with journal._transaction():
        session_row = journal._connection.execute(
            "SELECT session_id, repository_id, base_sha FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if session_row is not None:
        existing_repo_id = session_row[1]
        existing_base_sha = session_row[2]
        if existing_repo_id != repository_id or existing_base_sha != base_sha:
            with journal._transaction():
                issue_created = record_ingestion_issue_in_transaction(
                    journal,
                    source_id=source_id,
                    source_path=source_path,
                    snapshot_sha=snapshot_sha,
                    raw_sha256=raw_sha256,
                    error_code=BridgeErrorCode.SESSION_ID_COLLISION.value,
                    detail=f"Session collision for session {session_id}: existing repo/base ({existing_repo_id}/{existing_base_sha}) != manifest repo/base ({repository_id}/{base_sha})",
                    blocking=True,
                    document_commit_sha=manifest_commit_sha,
                    session_id=session_id,
                    created_at=now,
                )
            raise CollisionError(
                BridgeErrorCode.SESSION_ID_COLLISION,
                f"Session collision for session {session_id}",
                issue_created=issue_created,
            )

    outcome = PromotionOutcome(0, 0, ())
    with journal._transaction():
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

        # Promote staged commands for this session
        outcome = _promote_pending_commands_in_transaction(journal, session_id, now)

    rec = get_session_ingestion(journal, session_id)
    assert rec is not None

    if outcome.blocking_collisions:
        err = outcome.blocking_collisions[0]
        err.promotion_outcome = outcome
        raise err

    return rec, True, outcome


def record_ingested_command(
    journal: Journal,
    *,
    source_id: str,
    snapshot_sha: str,
    source_path: str,
    session_id: str,
    sequence: int,
    document_commit_sha: str,
    raw_content: bytes,
    raw_sha256_value: str,
) -> tuple[CommandIngestionRecord | None, bool, int]:
    journal._ensure_open()
    command_id = command_id_for(session_id, sequence)
    now = journal._now_fn()

    validate_sha40("snapshot_sha", snapshot_sha)
    validate_sha40("document_commit_sha", document_commit_sha)
    validate_sha64("raw_sha256_value", raw_sha256_value)

    collision_err: CollisionError | None = None
    created = False
    rec: CommandIngestionRecord | None = None
    issues_created = 0

    with journal._transaction():
        # O obecności manifestu decyduje session_ingestion
        session_ingestion_row = get_session_ingestion(journal, session_id)
        if session_ingestion_row is None:
            # Staging flow
            existing_staged = journal._connection.execute(
                """
                SELECT source_path, document_commit_sha, raw_sha256, content
                FROM pending_command_documents
                WHERE session_id = ? AND sequence = ?
                """,
                (session_id, sequence),
            ).fetchone()

            if existing_staged is not None:
                staged_path, staged_commit, staged_hash, staged_content = existing_staged
                if (
                    staged_path == source_path
                    and staged_commit == document_commit_sha
                    and staged_hash == raw_sha256_value
                    and staged_content == raw_content
                ):
                    journal._connection.execute(
                        """
                        UPDATE pending_command_documents
                        SET last_seen_at = ?
                        WHERE session_id = ? AND sequence = ?
                        """,
                        (now, session_id, sequence),
                    )
                    created = False
                else:
                    issue_created = record_ingestion_issue_in_transaction(
                        journal,
                        source_id=source_id,
                        source_path=source_path,
                        snapshot_sha=snapshot_sha,
                        raw_sha256=raw_sha256_value,
                        error_code=BridgeErrorCode.SEQUENCE_COLLISION.value,
                        detail=f"Staged sequence collision for session {session_id} sequence {sequence}",
                        blocking=True,
                        created_at=now,
                        document_commit_sha=document_commit_sha,
                        session_id=session_id,
                        command_id=command_id,
                    )
                    collision_err = CollisionError(
                        BridgeErrorCode.SEQUENCE_COLLISION,
                        f"Staged sequence collision for session {session_id} sequence {sequence}",
                        issue_created=issue_created,
                    )
            else:
                journal._connection.execute(
                    """
                    INSERT INTO pending_command_documents (
                        session_id, sequence, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256, content, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        sequence,
                        source_id,
                        snapshot_sha,
                        source_path,
                        document_commit_sha,
                        raw_sha256_value,
                        raw_content,
                        now,
                        now,
                    ),
                )
                created = True
        else:
            # Direct Ingestion Flow
            existing_ingestion = journal._connection.execute(
                """
                SELECT command_id, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256,
                       created_remote_at, expires_at, first_seen_at, last_seen_at
                FROM command_ingestion WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()

            existing_command = journal._get_command_row_for_update(command_id)

            if existing_ingestion is not None:
                if (
                    existing_ingestion[3] == source_path
                    and existing_ingestion[4] == document_commit_sha
                    and existing_ingestion[5] == raw_sha256_value
                ):
                    journal._connection.execute(
                        "UPDATE command_ingestion SET last_seen_at = ? WHERE command_id = ?",
                        (now, command_id),
                    )
                    rec = _row_to_command_ingestion(
                        (
                            existing_ingestion[0],
                            existing_ingestion[1],
                            existing_ingestion[2],
                            existing_ingestion[3],
                            existing_ingestion[4],
                            existing_ingestion[5],
                            existing_ingestion[6],
                            existing_ingestion[7],
                            existing_ingestion[8],
                            now,
                        )
                    )
                    created = False
                else:
                    issue_created = record_ingestion_issue_in_transaction(
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
                    collision_err = CollisionError(
                        BridgeErrorCode.COMMAND_ID_COLLISION,
                        f"Command document collision for {command_id}",
                        issue_created=issue_created,
                    )

            elif existing_command is not None:
                try:
                    decoded_content = raw_content.decode("utf-8", errors="strict")
                    content_matches = (existing_command[4] == decoded_content)
                except UnicodeDecodeError:
                    content_matches = False

                if (
                    existing_command[1] == session_id
                    and existing_command[2] == sequence
                    and content_matches
                    and existing_command[5] == document_commit_sha
                ):
                    journal._connection.execute(
                        """
                        INSERT INTO command_ingestion (
                            command_id, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256,
                            created_remote_at, expires_at, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                        """,
                        (command_id, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256_value, now, now),
                    )
                    rec = get_command_ingestion(journal, command_id)
                    created = False
                else:
                    issue_created = record_ingestion_issue_in_transaction(
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
                    collision_err = CollisionError(
                        BridgeErrorCode.SEQUENCE_COLLISION,
                        f"Sequence collision for session {session_id} sequence {sequence}",
                        issue_created=issue_created,
                    )
            else:
                seq_row = journal._connection.execute(
                    "SELECT command_id FROM commands WHERE session_id = ? AND sequence = ?",
                    (session_id, sequence),
                ).fetchone()
                if seq_row is not None and seq_row[0] != command_id:
                    issue_created = record_ingestion_issue_in_transaction(
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
                    collision_err = CollisionError(
                        BridgeErrorCode.SEQUENCE_COLLISION,
                        f"Sequence collision for session {session_id} sequence {sequence}",
                        issue_created=issue_created,
                    )
                else:
                    try:
                        decoded_content = raw_content.decode("utf-8", errors="strict")
                        journal._connection.execute(
                            """
                            INSERT INTO commands (
                                command_id, session_id, sequence, command_sha256, command_json,
                                command_commit_sha, state, expected_revision, expected_state_hash,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                            """,
                            (
                                command_id,
                                session_id,
                                sequence,
                                raw_sha256_value,
                                decoded_content,
                                document_commit_sha,
                                CommandState.DISCOVERED.value,
                                now,
                                now,
                            ),
                        )

                        journal._connection.execute(
                            """
                            INSERT INTO command_ingestion (
                                command_id, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256,
                                created_remote_at, expires_at, first_seen_at, last_seen_at
                            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                            """,
                            (command_id, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256_value, now, now),
                        )

                        journal._append_event_in_transaction(
                            session_id=session_id,
                            command_id=command_id,
                            event_type="command.discovered",
                            payload={"sequence": sequence, "source_path": source_path},
                            created_at=now,
                        )
                        rec = get_command_ingestion(journal, command_id)
                        created = True
                    except UnicodeDecodeError as decode_exc:
                        issue_created = record_ingestion_issue_in_transaction(
                            journal,
                            source_id=source_id,
                            source_path=source_path,
                            snapshot_sha=snapshot_sha,
                            raw_sha256=raw_sha256_value,
                            error_code=BridgeErrorCode.INVALID_PAYLOAD.value,
                            detail=f"UTF-8 decode failed for command: {decode_exc}",
                            blocking=False,
                            created_at=now,
                            document_commit_sha=document_commit_sha,
                            session_id=session_id,
                            command_id=command_id,
                        )
                        rec = None
                        created = False
                        issues_created = 1 if issue_created else 0

    if collision_err is not None:
        raise collision_err

    return rec, created, issues_created


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

    try:
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
                # Retrieve all predecessors
                predecessors = journal._connection.execute(
                    """
                    SELECT sequence, state FROM commands
                    WHERE session_id = ? AND sequence < ?
                    ORDER BY sequence ASC
                    """,
                    (target_session_id, sequence),
                ).fetchall()

                # Ensure all sequence indices 1..N-1 exist and there are no gaps
                if len(predecessors) != sequence - 1:
                    return None

                for seq_idx, state_str in predecessors:
                    pred_state = CommandState(state_str)
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

                # Verify unique sessions constraint
                active = journal._connection.execute(
                    "SELECT session_id FROM sessions WHERE state IN ('active', 'completing') AND session_id != ?",
                    (target_session_id,),
                ).fetchone()
                if active is not None:
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_CONFLICT,
                        f"Another session {active[0]} is already active or completing",
                    )

                try:
                    updated_session = journal._connection.execute(
                        """
                        UPDATE sessions SET state = ?, updated_at = ?
                        WHERE session_id = ? AND state = ?
                        """,
                        (SessionState.ACTIVE.value, now, target_session_id, SessionState.CREATED.value),
                    )
                except sqlite3.IntegrityError as exc:
                    if "idx_sessions_one_active" in str(exc).lower():
                        raise BridgeError(
                            BridgeErrorCode.JOURNAL_CONFLICT,
                            f"Concurrency conflict: another session is already active or completing: {exc}",
                        ) from exc
                    raise

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

            # Verify unique workers constraint
            active_worker = journal._connection.execute(
                "SELECT command_id FROM commands WHERE state IN ('claimed', 'executing', 'effect_recorded') AND command_id != ?",
                (command_id,),
            ).fetchone()
            if active_worker is not None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Another command {active_worker[0]} is already active/worker",
                )

            try:
                updated_command = journal._connection.execute(
                    """
                    UPDATE commands SET state = ?, updated_at = ?
                    WHERE command_id = ? AND state = ?
                    """,
                    (CommandState.CLAIMED.value, now, command_id, CommandState.VALIDATED.value),
                )
            except sqlite3.IntegrityError as exc:
                if "idx_commands_one_worker" in str(exc).lower():
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_CONFLICT,
                        f"Concurrency conflict: another command is already active/worker: {exc}",
                    ) from exc
                raise

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
    except BridgeError:
        raise
    except sqlite3.Error as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            f"SQLite error during claim: {exc}",
        ) from exc


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
        journal.expire_session_and_pending_commands(record.session_id, record.expires_at)


def _promote_pending_commands_in_transaction(journal: Journal, session_id: str, now: str) -> PromotionOutcome:
    pending_rows = journal._connection.execute(
        """
        SELECT sequence, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256, content, first_seen_at, last_seen_at
        FROM pending_command_documents
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchall()

    promoted_count = 0
    issues_created = 0
    blocking_collisions = []
    for seq, src_id, snap_sha, src_path, doc_commit_sha, r_sha256, content, first_seen, last_seen in pending_rows:
        command_id = command_id_for(session_id, seq)
        existing_row = journal._connection.execute(
            "SELECT 1 FROM commands WHERE session_id = ? AND sequence = ?",
            (session_id, seq),
        ).fetchone()
        if existing_row is not None:
            issue_created = record_ingestion_issue_in_transaction(
                journal,
                source_id=src_id,
                source_path=src_path,
                snapshot_sha=snap_sha,
                raw_sha256=r_sha256,
                error_code=BridgeErrorCode.SEQUENCE_COLLISION.value,
                detail=f"Sequence collision during staged promotion for session {session_id} sequence {seq}",
                blocking=True,
                created_at=now,
                document_commit_sha=doc_commit_sha,
                session_id=session_id,
                command_id=command_id,
            )
            if issue_created:
                issues_created += 1
            blocking_collisions.append(
                CollisionError(
                    BridgeErrorCode.SEQUENCE_COLLISION,
                    f"Sequence collision during staged promotion for session {session_id} sequence {seq}",
                    issue_created=issue_created,
                )
            )
            continue

        if isinstance(content, str):
            decoded_content = content
        else:
            try:
                decoded_content = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as decode_exc:
                issue_created = record_ingestion_issue_in_transaction(
                    journal,
                    source_id=src_id,
                    source_path=src_path,
                    snapshot_sha=snap_sha,
                    raw_sha256=r_sha256,
                    error_code=BridgeErrorCode.INVALID_PAYLOAD.value,
                    detail=f"UTF-8 decode failed for staged command: {decode_exc}",
                    blocking=False,
                    created_at=now,
                    document_commit_sha=doc_commit_sha,
                    session_id=session_id,
                    command_id=command_id,
                )
                if issue_created:
                    issues_created += 1
                continue

        journal._connection.execute(
            """
            INSERT INTO commands (
                command_id, session_id, sequence, command_sha256, command_json,
                command_commit_sha, state, expected_revision, expected_state_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                command_id,
                session_id,
                seq,
                r_sha256,
                decoded_content,
                doc_commit_sha,
                CommandState.DISCOVERED.value,
                first_seen,
                last_seen,
            ),
        )

        journal._connection.execute(
            """
            INSERT INTO command_ingestion (
                command_id, source_id, snapshot_sha, source_path, document_commit_sha, raw_sha256,
                created_remote_at, expires_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (command_id, src_id, snap_sha, src_path, doc_commit_sha, r_sha256, first_seen, last_seen),
        )

        journal._append_event_in_transaction(
            session_id=session_id,
            command_id=command_id,
            event_type="command.discovered",
            payload={"sequence": seq, "source_path": src_path},
            created_at=now,
        )

        journal._connection.execute(
            "DELETE FROM pending_command_documents WHERE session_id = ? AND sequence = ?",
            (session_id, seq),
        )
        promoted_count += 1

    return PromotionOutcome(
        promoted_count=promoted_count,
        issues_created=issues_created,
        blocking_collisions=tuple(blocking_collisions),
    )


def _add_seconds_iso(iso_timestamp: str, seconds: float) -> str:
    parsed = parse_strict_utc_timestamp(iso_timestamp, field="timestamp")
    from datetime import timedelta

    updated = parsed + timedelta(seconds=seconds)
    return updated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# New validation/expiry transaction methods

def expire_session_and_pending_commands(journal: Journal, session_id: str, expires_at: str) -> None:
    journal._ensure_open()
    now = journal._now_fn()
    with journal._transaction():
        session_row = journal._get_session_row_for_update(session_id)
        if session_row is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Session not found: {session_id}",
            )

        from .journal import _parse_session_state
        session_state = _parse_session_state(session_row[3])

        if session_state == SessionState.CREATED:
            validate_session_transition(SessionState.CREATED, SessionState.ABORTED)
            journal._conn.execute(
                """
                UPDATE sessions
                SET state = ?, updated_at = ?
                WHERE session_id = ? AND state = ?
                """,
                (SessionState.ABORTED.value, now, session_id, SessionState.CREATED.value),
            )
            journal._append_event_in_transaction(
                session_id=session_id,
                event_type="session.state_changed",
                payload={
                    "from_state": SessionState.CREATED.value,
                    "to_state": SessionState.ABORTED.value,
                },
                created_at=now,
            )
        elif session_state != SessionState.ABORTED:
            # If the session is not CREATED and not already ABORTED, do not touch commands either
            return

        # Expire all DISCOVERED commands of this session
        discovered_rows = journal._conn.execute(
            """
            SELECT command_id, sequence, state
            FROM commands
            WHERE session_id = ? AND state = ?
            """,
            (session_id, CommandState.DISCOVERED.value),
        ).fetchall()

        for cmd_id, seq, state_val in discovered_rows:
            cmd_state = CommandState(state_val)
            validate_command_transition(cmd_state, CommandState.EXPIRED)
            journal._conn.execute(
                """
                UPDATE commands
                SET state = ?, updated_at = ?
                WHERE command_id = ? AND state = ?
                """,
                (CommandState.EXPIRED.value, now, cmd_id, CommandState.DISCOVERED.value),
            )
            journal._append_event_in_transaction(
                session_id=session_id,
                command_id=cmd_id,
                event_type="command.expired",
                payload={"sequence": seq, "state": CommandState.EXPIRED.value},
                created_at=now,
            )
            journal._append_event_in_transaction(
                session_id=session_id,
                command_id=cmd_id,
                event_type="command.state_changed",
                payload={
                    "from_state": CommandState.DISCOVERED.value,
                    "to_state": CommandState.EXPIRED.value,
                },
                created_at=now,
            )


def validate_and_update_command(
    journal: Journal,
    command_id: str,
    *,
    command_json: str,
    command_sha256: str,
    expected_revision: int | None,
    expected_state_hash: str | None,
    created_remote_at: str,
    expires_at: str,
    snapshot_sha: str,
) -> None:
    journal._ensure_open()
    validate_sha40("snapshot_sha", snapshot_sha)
    now = journal._now_fn()
    with journal._transaction():
        # 1. CAS verification: check command state is DISCOVERED
        row = journal._get_command_row_for_update(command_id)
        if row is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Command not found: {command_id}",
            )
        current_state = CommandState(row[6])
        if current_state != CommandState.DISCOVERED:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Command state mismatch: expected DISCOVERED, got {current_state.value}",
            )

        # 2. Check ingestion metadata
        ing_row = journal._conn.execute(
            "SELECT source_id, snapshot_sha FROM command_ingestion WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if ing_row is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Command ingestion metadata not found for {command_id}",
            )

        # 3. Update commands
        updated_cmd = journal._conn.execute(
            """
            UPDATE commands
            SET command_json = ?, command_sha256 = ?, expected_revision = ?,
                expected_state_hash = ?, state = ?, updated_at = ?
            WHERE command_id = ? AND state = ?
            """,
            (
                command_json,
                command_sha256,
                expected_revision,
                expected_state_hash,
                CommandState.VALIDATED.value,
                now,
                command_id,
                CommandState.DISCOVERED.value,
            ),
        )
        if updated_cmd.rowcount != 1:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Failed to update command {command_id} to VALIDATED",
            )

        # 4. Update command_ingestion metadata
        updated_ing = journal._conn.execute(
            """
            UPDATE command_ingestion
            SET created_remote_at = ?, expires_at = ?, snapshot_sha = ?, last_seen_at = ?
            WHERE command_id = ?
            """,
            (created_remote_at, expires_at, snapshot_sha, now, command_id),
        )
        if updated_ing.rowcount != 1:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Failed to update ingestion metadata for command {command_id}",
            )

        # 5. Append events
        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type="command.validated",
            payload={"sequence": row[2], "state": CommandState.VALIDATED.value},
            created_at=now,
        )
        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type="command.state_changed",
            payload={
                "from_state": CommandState.DISCOVERED.value,
                "to_state": CommandState.VALIDATED.value,
            },
            created_at=now,
        )


def reject_command_during_validation(
    journal: Journal,
    command_id: str,
    *,
    error_code: str,
    detail: str,
    source_id: str,
    source_path: str,
    snapshot_sha: str,
    raw_sha256: str,
    document_commit_sha: str | None,
) -> bool:
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
        if current_state != CommandState.DISCOVERED:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Command state mismatch: expected DISCOVERED, got {current_state.value}",
            )

        # Update commands state to REJECTED
        journal._conn.execute(
            "UPDATE commands SET state = ?, updated_at = ? WHERE command_id = ?",
            (CommandState.REJECTED.value, now, command_id),
        )

        # Record issue atomically
        issue_created = record_ingestion_issue_in_transaction(
            journal,
            source_id=source_id,
            source_path=source_path,
            snapshot_sha=snapshot_sha,
            raw_sha256=raw_sha256,
            error_code=error_code,
            detail=detail,
            blocking=False,
            created_at=now,
            document_commit_sha=document_commit_sha,
            session_id=row[1],
            command_id=command_id,
        )

        # Append events
        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type="command.rejected",
            payload={"sequence": row[2], "state": CommandState.REJECTED.value},
            created_at=now,
        )
        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type="command.state_changed",
            payload={
                "from_state": CommandState.DISCOVERED.value,
                "to_state": CommandState.REJECTED.value,
            },
            created_at=now,
        )
        return issue_created


def expire_command_during_validation(journal: Journal, command_id: str) -> None:
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
        if current_state != CommandState.DISCOVERED:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Command state mismatch: expected DISCOVERED, got {current_state.value}",
            )

        journal._conn.execute(
            "UPDATE commands SET state = ?, updated_at = ? WHERE command_id = ?",
            (CommandState.EXPIRED.value, now, command_id),
        )

        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type="command.expired",
            payload={"sequence": row[2], "state": CommandState.EXPIRED.value},
            created_at=now,
        )
        journal._append_event_in_transaction(
            session_id=row[1],
            command_id=command_id,
            event_type="command.state_changed",
            payload={
                "from_state": CommandState.DISCOVERED.value,
                "to_state": CommandState.EXPIRED.value,
            },
            created_at=now,
        )


def count_session_commands_before(journal: Journal, session_id: str, sequence: int) -> int:
    journal._ensure_open()
    row = journal._connection.execute(
        "SELECT COUNT(*) FROM commands WHERE session_id = ? AND sequence < ?",
        (session_id, sequence),
    ).fetchone()
    return row[0] if row is not None else 0
