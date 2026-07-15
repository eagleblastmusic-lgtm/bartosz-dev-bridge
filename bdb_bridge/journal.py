from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .migrations import apply_migrations, utc_now_iso
from .models import (
    BridgeErrorCode,
    CommandRecord,
    CommandState,
    JournalEvent,
    ResultRecord,
    ResultStatus,
    SessionRecord,
    SessionState,
    WorkspaceRecord,
    validate_command_transition,
    validate_session_transition,
)
from .protocol import BridgeError, result_path_for, validate_session_id
from .serializers import MAX_RESULT_BYTES, canonical_json, sha256_text


class Journal:
    def __init__(
        self,
        conn: sqlite3.Connection,
        path: Path,
        *,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        self._conn = conn
        self._path = path
        self._now_fn = now_fn or utc_now_iso
        self._closed = False

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        now_fn: Callable[[], str] | None = None,
    ) -> Journal:
        db_path = Path(path)
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                db_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            _configure_connection(conn)
            journal = cls(conn, db_path, now_fn=now_fn)
            journal.migrate()
            return journal
        except BridgeError:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise
        except sqlite3.Error as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                f"Failed to open journal database: {exc}",
            ) from exc
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _connection(self) -> sqlite3.Connection:
        self._ensure_open()
        return self._conn

    def migrate(self) -> None:
        self._ensure_open()
        apply_migrations(self._conn, now_fn=self._now_fn)

    def create_session(
        self,
        session_id: str,
        repository_id: str,
        base_sha: str,
        *,
        state: SessionState = SessionState.CREATED,
    ) -> SessionRecord:
        self._ensure_open()
        validate_session_id(session_id)
        _require_non_empty_str(repository_id, "repository_id")
        _require_non_empty_str(base_sha, "base_sha")
        if state is not SessionState.CREATED:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "New sessions must start in created state",
            )

        now = self._now_fn()
        with self._transaction():
            try:
                self._conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, repository_id, base_sha, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        repository_id,
                        base_sha,
                        state.value,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Session already exists: {session_id}",
                ) from exc
            self._append_event_in_transaction(
                session_id=session_id,
                event_type="session.created",
                payload={"state": state.value},
                created_at=now,
            )
        record = self.get_session(session_id)
        assert record is not None
        return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        self._ensure_open()
        row = self._conn.execute(
            """
            SELECT session_id, repository_id, base_sha, state, created_at, updated_at
            FROM sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionRecord(
            session_id=row[0],
            repository_id=row[1],
            base_sha=row[2],
            state=_parse_session_state(row[3]),
            created_at=row[4],
            updated_at=row[5],
        )

    def transition_session(
        self,
        session_id: str,
        expected_state: SessionState,
        new_state: SessionState,
    ) -> SessionRecord:
        self._ensure_open()
        now = self._now_fn()
        with self._transaction():
            current = self._get_session_row_for_update(session_id)
            if current is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Session not found: {session_id}",
                )
            current_state = _parse_session_state(current[3])
            if current_state != expected_state:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Session state mismatch: expected {expected_state.value}, got {current_state.value}",
                )
            validate_session_transition(current_state, new_state)
            updated = self._conn.execute(
                """
                UPDATE sessions
                SET state = ?, updated_at = ?
                WHERE session_id = ? AND state = ?
                """,
                (new_state.value, now, session_id, expected_state.value),
            )
            if updated.rowcount != 1:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Failed to transition session {session_id}",
                )
            self._append_event_in_transaction(
                session_id=session_id,
                event_type="session.state_changed",
                payload={
                    "from_state": expected_state.value,
                    "to_state": new_state.value,
                },
                created_at=now,
            )
        record = self.get_session(session_id)
        assert record is not None
        return record

    def record_command(
        self,
        session_id: str,
        command_id: str,
        sequence: int,
        command: dict[str, Any],
        *,
        command_commit_sha: str | None = None,
        expected_revision: int | None = None,
        expected_state_hash: str | None = None,
    ) -> CommandRecord:
        self._ensure_open()
        validate_session_id(session_id)
        _require_non_empty_str(command_id, "command_id")
        sequence = _require_positive_int(sequence, "sequence")
        command_json = _canonical_command_json(command)
        command_sha256 = sha256_text(command_json)
        _validate_command_identity(command, session_id=session_id, command_id=command_id, sequence=sequence)
        if command_commit_sha is not None:
            _require_non_empty_str(command_commit_sha, "command_commit_sha")
        if expected_state_hash is not None:
            _require_non_empty_str(expected_state_hash, "expected_state_hash")
        if expected_revision is not None:
            expected_revision = _require_non_negative_int(expected_revision, "expected_revision")

        now = self._now_fn()
        with self._transaction():
            existing = self._get_command_row_for_update(command_id)
            if existing is not None:
                if not _command_metadata_matches(
                    existing,
                    session_id=session_id,
                    sequence=sequence,
                    command_json=command_json,
                    command_sha256=command_sha256,
                    command_commit_sha=command_commit_sha,
                    expected_revision=expected_revision,
                    expected_state_hash=expected_state_hash,
                ):
                    raise BridgeError(
                        BridgeErrorCode.COMMAND_ID_COLLISION,
                        f"Command ID collision for {command_id}",
                    )
                return _row_to_command(existing)

            sequence_row = self._conn.execute(
                """
                SELECT command_id, session_id, sequence, command_sha256, command_json,
                       command_commit_sha, state, expected_revision, expected_state_hash,
                       created_at, updated_at
                FROM commands
                WHERE session_id = ? AND sequence = ?
                """,
                (session_id, sequence),
            ).fetchone()
            if sequence_row is not None:
                raise BridgeError(
                    BridgeErrorCode.SEQUENCE_COLLISION,
                    f"Sequence {sequence} already used in session {session_id}",
                )

            try:
                self._conn.execute(
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
                        command_json,
                        command_commit_sha,
                        CommandState.DISCOVERED.value,
                        expected_revision,
                        expected_state_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _map_integrity_error(exc, context="record_command") from exc

            self._append_event_in_transaction(
                session_id=session_id,
                command_id=command_id,
                event_type="command.recorded",
                payload={"sequence": sequence, "state": CommandState.DISCOVERED.value},
                created_at=now,
            )

        record = self.get_command(command_id)
        assert record is not None
        return record

    def get_command(self, command_id: str) -> CommandRecord | None:
        self._ensure_open()
        row = self._conn.execute(
            """
            SELECT command_id, session_id, sequence, command_sha256, command_json,
                   command_commit_sha, state, expected_revision, expected_state_hash,
                   created_at, updated_at
            FROM commands WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_command(row)

    def get_command_by_sequence(self, session_id: str, sequence: int) -> CommandRecord | None:
        self._ensure_open()
        sequence = _require_positive_int(sequence, "sequence")
        row = self._conn.execute(
            """
            SELECT command_id, session_id, sequence, command_sha256, command_json,
                   command_commit_sha, state, expected_revision, expected_state_hash,
                   created_at, updated_at
            FROM commands WHERE session_id = ? AND sequence = ?
            """,
            (session_id, sequence),
        ).fetchone()
        if row is None:
            return None
        return _row_to_command(row)

    def transition_command(
        self,
        command_id: str,
        expected_state: CommandState,
        new_state: CommandState,
    ) -> CommandRecord:
        self._ensure_open()
        now = self._now_fn()
        with self._transaction():
            row = self._get_command_row_for_update(command_id)
            if row is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Command not found: {command_id}",
                )
            current_state = _parse_command_state(row[6])
            if current_state != expected_state:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Command state mismatch: expected {expected_state.value}, got {current_state.value}",
                )
            validate_command_transition(current_state, new_state)
            updated = self._conn.execute(
                """
                UPDATE commands
                SET state = ?, updated_at = ?
                WHERE command_id = ? AND state = ?
                """,
                (new_state.value, now, command_id, expected_state.value),
            )
            if updated.rowcount != 1:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Failed to transition command {command_id}",
                )
            self._append_event_in_transaction(
                session_id=row[1],
                command_id=command_id,
                event_type="command.state_changed",
                payload={
                    "from_state": expected_state.value,
                    "to_state": new_state.value,
                },
                created_at=now,
            )
        record = self.get_command(command_id)
        assert record is not None
        return record

    def register_workspace(
        self,
        session_id: str,
        workspace_path: str,
        base_sha: str,
        revision: int,
        state_hash: str,
    ) -> WorkspaceRecord:
        self._ensure_open()
        validate_session_id(session_id)
        _require_non_empty_str(workspace_path, "workspace_path")
        _require_non_empty_str(base_sha, "base_sha")
        _require_non_empty_str(state_hash, "state_hash")
        revision = _require_non_negative_int(revision, "revision")

        now = self._now_fn()
        with self._transaction():
            try:
                self._conn.execute(
                    """
                    INSERT INTO workspaces (
                        session_id, workspace_path, base_sha, revision, state_hash,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        workspace_path,
                        base_sha,
                        revision,
                        state_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _map_integrity_error(exc, context="register_workspace") from exc
            self._append_event_in_transaction(
                session_id=session_id,
                event_type="workspace.registered",
                payload={"workspace_path": workspace_path, "revision": revision},
                created_at=now,
            )
        record = self.get_workspace(session_id)
        assert record is not None
        return record

    def get_workspace(self, session_id: str) -> WorkspaceRecord | None:
        self._ensure_open()
        row = self._conn.execute(
            """
            SELECT session_id, workspace_path, base_sha, revision, state_hash, created_at, updated_at
            FROM workspaces WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return WorkspaceRecord(
            session_id=row[0],
            workspace_path=row[1],
            base_sha=row[2],
            revision=row[3],
            state_hash=row[4],
            created_at=row[5],
            updated_at=row[6],
        )

    def update_workspace_state(
        self,
        session_id: str,
        expected_revision: int,
        expected_state_hash: str,
        new_revision: int,
        new_state_hash: str,
    ) -> WorkspaceRecord:
        self._ensure_open()
        expected_revision = _require_non_negative_int(expected_revision, "expected_revision")
        new_revision = _require_non_negative_int(new_revision, "new_revision")
        _require_non_empty_str(expected_state_hash, "expected_state_hash")
        _require_non_empty_str(new_state_hash, "new_state_hash")
        if new_revision != expected_revision + 1:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                f"Workspace revision must advance by exactly one: {expected_revision} -> {new_revision}",
            )

        now = self._now_fn()
        with self._transaction():
            existing = self._conn.execute(
                """
                SELECT session_id FROM workspaces WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Workspace not found for session {session_id}",
                )
            updated = self._conn.execute(
                """
                UPDATE workspaces
                SET revision = ?, state_hash = ?, updated_at = ?
                WHERE session_id = ? AND revision = ? AND state_hash = ?
                """,
                (
                    new_revision,
                    new_state_hash,
                    now,
                    session_id,
                    expected_revision,
                    expected_state_hash,
                ),
            )
            if updated.rowcount != 1:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Workspace state conflict for session {session_id}",
                )
            self._append_event_in_transaction(
                session_id=session_id,
                event_type="workspace.state_updated",
                payload={
                    "from_revision": expected_revision,
                    "to_revision": new_revision,
                    "from_state_hash": expected_state_hash,
                    "to_state_hash": new_state_hash,
                },
                created_at=now,
            )
        record = self.get_workspace(session_id)
        assert record is not None
        return record

    def store_result(
        self,
        command_id: str,
        result_json: str,
        remote_path: str,
    ) -> ResultRecord:
        self._ensure_open()
        _require_non_empty_str(command_id, "command_id")
        _require_non_empty_str(result_json, "result_json")
        _require_non_empty_str(remote_path, "remote_path")

        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise BridgeError(
                BridgeErrorCode.RESULT_TOO_LARGE,
                f"Result exceeds {MAX_RESULT_BYTES} bytes",
            )

        parsed = _parse_result_json(result_json)
        status = _extract_result_status(parsed)
        error_code = _derive_error_code(status)

        with self._transaction():
            command_row = self._get_command_row_for_update(command_id)
            if command_row is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Command not found: {command_id}",
                )
            session_id = command_row[1]
            sequence = command_row[2]
            _validate_result_metadata(
                parsed,
                command_id=command_id,
                session_id=session_id,
                sequence=sequence,
            )
            expected_remote_path = result_path_for(session_id, sequence)
            if remote_path != expected_remote_path:
                raise BridgeError(
                    BridgeErrorCode.INVALID_PAYLOAD,
                    f"remote_path must be {expected_remote_path}",
                )

            result_sha256 = sha256_text(result_json)
            now = self._now_fn()
            existing = self._get_result_row_for_update(command_id)
            if existing is not None:
                candidate = _row_to_result(existing)
                if _result_record_matches(
                    candidate,
                    command_id=command_id,
                    session_id=session_id,
                    sequence=sequence,
                    status=status,
                    error_code=error_code,
                    result_sha256=result_sha256,
                    result_json=result_json,
                    remote_path=remote_path,
                ):
                    return candidate
                raise BridgeError(
                    BridgeErrorCode.RESULT_COLLISION,
                    f"Result collision for command {command_id}",
                )

            sequence_row = self._conn.execute(
                """
                SELECT command_id FROM results WHERE session_id = ? AND sequence = ?
                """,
                (session_id, sequence),
            ).fetchone()
            if sequence_row is not None and sequence_row[0] != command_id:
                raise BridgeError(
                    BridgeErrorCode.RESULT_COLLISION,
                    f"Result sequence collision for session {session_id} sequence {sequence}",
                )

            try:
                self._conn.execute(
                    """
                    INSERT INTO results (
                        command_id, session_id, sequence, status, error_code,
                        result_sha256, result_json, remote_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        session_id,
                        sequence,
                        status,
                        error_code,
                        result_sha256,
                        result_json,
                        remote_path,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _map_integrity_error(exc, context="store_result") from exc

            self._append_event_in_transaction(
                session_id=session_id,
                command_id=command_id,
                event_type="result.stored",
                payload={"sequence": sequence, "status": status},
                created_at=now,
            )

        record = self.get_result(command_id)
        assert record is not None
        return record

    def get_result(self, command_id: str) -> ResultRecord | None:
        self._ensure_open()
        row = self._conn.execute(
            """
            SELECT command_id, session_id, sequence, status, error_code,
                   result_sha256, result_json, remote_path, created_at
            FROM results WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_result(row)

    def append_event(
        self,
        *,
        session_id: str | None = None,
        command_id: str | None = None,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> JournalEvent:
        self._ensure_open()
        _require_non_empty_str(event_type, "event_type")
        if session_id is not None:
            validate_session_id(session_id)
        if command_id is not None:
            _require_non_empty_str(command_id, "command_id")
        now = self._now_fn()
        with self._transaction():
            event_id = self._append_event_in_transaction(
                session_id=session_id,
                command_id=command_id,
                event_type=event_type,
                payload=payload,
                created_at=now,
            )
        return self._get_event(event_id)

    def list_events(
        self,
        *,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> list[JournalEvent]:
        self._ensure_open()
        query = """
            SELECT event_id, session_id, command_id, event_type, payload_json, created_at
            FROM events
        """
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if command_id is not None:
            clauses.append("command_id = ?")
            params.append(command_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_id ASC"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def _append_event_in_transaction(
        self,
        *,
        session_id: str | None = None,
        command_id: str | None = None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        created_at: str,
    ) -> int:
        payload_json: str | None
        if payload is None:
            payload_json = None
        else:
            try:
                payload_json = canonical_json(payload)
            except (TypeError, ValueError) as exc:
                raise BridgeError(
                    BridgeErrorCode.INVALID_PAYLOAD,
                    "Event payload must be JSON-serializable",
                ) from exc
        cursor = self._conn.execute(
            """
            INSERT INTO events (session_id, command_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, command_id, event_type, payload_json, created_at),
        )
        return int(cursor.lastrowid)

    def _get_event(self, event_id: int) -> JournalEvent:
        row = self._conn.execute(
            """
            SELECT event_id, session_id, command_id, event_type, payload_json, created_at
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        assert row is not None
        return _row_to_event(row)

    def _get_session_row_for_update(self, session_id: str) -> sqlite3.Row | tuple[Any, ...] | None:
        return self._conn.execute(
            """
            SELECT session_id, repository_id, base_sha, state, created_at, updated_at
            FROM sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

    def _get_command_row_for_update(self, command_id: str) -> tuple[Any, ...] | None:
        return self._conn.execute(
            """
            SELECT command_id, session_id, sequence, command_sha256, command_json,
                   command_commit_sha, state, expected_revision, expected_state_hash,
                   created_at, updated_at
            FROM commands WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()

    def _get_result_row_for_update(self, command_id: str) -> tuple[Any, ...] | None:
        return self._conn.execute(
            """
            SELECT command_id, session_id, sequence, status, error_code,
                   result_sha256, result_json, remote_path, created_at
            FROM results WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()

    def _ensure_open(self) -> None:
        if self._closed:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Journal connection is closed",
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        if self._conn.in_transaction:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Nested transactions are not supported",
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        else:
            self._conn.commit()
        finally:
            if self._conn.in_transaction:
                self._conn.rollback()


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()
    if foreign_keys is None or foreign_keys[0] != 1:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Failed to enable SQLite foreign keys",
        )
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
    if journal_mode is None or str(journal_mode[0]).lower() != "wal":
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Failed to enable SQLite WAL journal mode",
        )
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()
    if busy_timeout is None or int(busy_timeout[0]) != 5000:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Failed to set SQLite busy_timeout to 5000",
        )
    synchronous = conn.execute("PRAGMA synchronous").fetchone()
    if synchronous is None or int(synchronous[0]) != 1:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            "Failed to set SQLite synchronous mode to NORMAL",
        )


def _require_non_empty_str(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{field} must be a non-empty string",
        )
    return value


def _require_positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{field} must be a positive integer",
        )
    return value


def _require_non_negative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{field} must be a non-negative integer",
        )
    return value


def _canonical_command_json(command: dict[str, Any]) -> str:
    if not isinstance(command, dict):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "command must be a JSON object",
        )
    try:
        return canonical_json(command)
    except (TypeError, ValueError) as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "command must be JSON-serializable",
        ) from exc


def _validate_command_identity(
    command: dict[str, Any],
    *,
    session_id: str,
    command_id: str,
    sequence: int,
) -> None:
    for field, expected in (
        ("session_id", session_id),
        ("command_id", command_id),
        ("sequence", sequence),
    ):
        actual = command.get(field)
        if actual != expected:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"command {field} does not match argument value",
            )


def _command_metadata_matches(
    row: tuple[Any, ...],
    *,
    session_id: str,
    sequence: int,
    command_json: str,
    command_sha256: str,
    command_commit_sha: str | None,
    expected_revision: int | None,
    expected_state_hash: str | None,
) -> bool:
    return (
        row[1] == session_id
        and row[2] == sequence
        and row[4] == command_json
        and row[3] == command_sha256
        and row[5] == command_commit_sha
        and row[7] == expected_revision
        and row[8] == expected_state_hash
    )


def _parse_result_json(result_json: str) -> dict[str, Any]:
    try:
        parsed = json.loads(result_json)
    except json.JSONDecodeError as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "result_json must be valid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "result_json must be a JSON object",
        )
    return parsed


def _extract_result_status(parsed: dict[str, Any]) -> str:
    status = parsed.get("status")
    if not isinstance(status, str) or not status:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "result status must be a non-empty string",
        )
    return status


def _derive_error_code(status: str) -> str | None:
    result_status_values = {member.value for member in ResultStatus}
    bridge_error_values = {member.value for member in BridgeErrorCode}
    if status in result_status_values:
        return None
    if status in bridge_error_values:
        return status
    raise BridgeError(
        BridgeErrorCode.INVALID_PAYLOAD,
        f"Unknown result status: {status}",
    )


def _validate_result_metadata(
    parsed: dict[str, Any],
    *,
    command_id: str,
    session_id: str,
    sequence: int,
) -> None:
    for field, expected in (
        ("command_id", command_id),
        ("session_id", session_id),
        ("sequence", sequence),
    ):
        actual = parsed.get(field)
        if actual != expected:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"result {field} does not match command record",
            )


def _result_record_matches(
    record: ResultRecord,
    *,
    command_id: str,
    session_id: str,
    sequence: int,
    status: str,
    error_code: str | None,
    result_sha256: str,
    result_json: str,
    remote_path: str,
) -> bool:
    return (
        record.command_id == command_id
        and record.session_id == session_id
        and record.sequence == sequence
        and record.status == status
        and record.error_code == error_code
        and record.result_sha256 == result_sha256
        and record.result_json == result_json
        and record.remote_path == remote_path
    )


def _parse_command_state(value: str) -> CommandState:
    try:
        return CommandState(value)
    except ValueError as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            f"Unknown command state in database: {value}",
        ) from exc


def _parse_session_state(value: str) -> SessionState:
    try:
        return SessionState(value)
    except ValueError as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            f"Unknown session state in database: {value}",
        ) from exc


def _row_to_command(row: tuple[Any, ...]) -> CommandRecord:
    return CommandRecord(
        command_id=row[0],
        session_id=row[1],
        sequence=row[2],
        command_sha256=row[3],
        command_json=row[4],
        command_commit_sha=row[5],
        state=_parse_command_state(row[6]),
        expected_revision=row[7],
        expected_state_hash=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def _row_to_result(row: tuple[Any, ...]) -> ResultRecord:
    return ResultRecord(
        command_id=row[0],
        session_id=row[1],
        sequence=row[2],
        status=row[3],
        error_code=row[4],
        result_sha256=row[5],
        result_json=row[6],
        remote_path=row[7],
        created_at=row[8],
    )


def _row_to_event(row: tuple[Any, ...]) -> JournalEvent:
    return JournalEvent(
        event_id=row[0],
        session_id=row[1],
        command_id=row[2],
        event_type=row[3],
        payload_json=row[4],
        created_at=row[5],
    )


def _map_integrity_error(exc: sqlite3.IntegrityError, *, context: str) -> BridgeError:
    message = str(exc).lower()
    if "foreign key" in message:
        return BridgeError(
            BridgeErrorCode.JOURNAL_CONFLICT,
            f"Foreign key constraint violated during {context}",
        )
    if "check constraint" in message:
        return BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"Check constraint violated during {context}",
        )
    if "unique" in message:
        return BridgeError(
            BridgeErrorCode.JOURNAL_CONFLICT,
            f"Unique constraint violated during {context}",
        )
    return BridgeError(
        BridgeErrorCode.JOURNAL_CONFLICT,
        f"Integrity constraint violated during {context}",
    )
