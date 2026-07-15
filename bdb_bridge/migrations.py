from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .models import BridgeErrorCode
from .protocol import BridgeError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

JOURNAL_TABLES = frozenset(
    {
        "schema_migrations",
        "sessions",
        "commands",
        "workspaces",
        "results",
        "events",
        "ingestion_sources",
        "session_ingestion",
        "command_ingestion",
        "ingestion_issues",
    }
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    def checksum(self) -> str:
        payload = "\n".join(self.statements).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


MIGRATION_V1_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
)""",
    """CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  repository_id TEXT NOT NULL,
  base_sha TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""",
    """CREATE TABLE commands (
  command_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  command_sha256 TEXT NOT NULL,
  command_json TEXT NOT NULL,
  command_commit_sha TEXT,
  state TEXT NOT NULL,
  expected_revision INTEGER,
  expected_state_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id),
  UNIQUE (session_id, sequence)
)""",
    """CREATE TABLE workspaces (
  session_id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL UNIQUE,
  base_sha TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision >= 0),
  state_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
)""",
    """CREATE TABLE results (
  command_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  status TEXT NOT NULL,
  error_code TEXT,
  result_sha256 TEXT NOT NULL,
  result_json TEXT NOT NULL,
  remote_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (command_id) REFERENCES commands(command_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id),
  UNIQUE (session_id, sequence)
)""",
    """CREATE TABLE events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  command_id TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT,
  created_at TEXT NOT NULL
)""",
    "CREATE INDEX idx_events_session ON events(session_id, event_id)",
    "CREATE INDEX idx_events_command ON events(command_id, event_id)",
    """CREATE TRIGGER events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END""",
    """CREATE TRIGGER events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END""",
)

MIGRATION_V2_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE ingestion_sources (
  source_id TEXT PRIMARY KEY,
  last_observed_sha TEXT,
  attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
  next_attempt_at TEXT,
  last_error TEXT,
  last_success_at TEXT,
  updated_at TEXT NOT NULL
)""",
    """CREATE TABLE session_ingestion (
  session_id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  manifest_commit_sha TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  created_remote_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
)""",
    """CREATE TABLE command_ingestion (
  command_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  snapshot_sha TEXT NOT NULL,
  source_path TEXT NOT NULL,
  document_commit_sha TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  created_remote_at TEXT,
  expires_at TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  FOREIGN KEY (command_id) REFERENCES commands(command_id)
)""",
    """CREATE TABLE pending_command_documents (
  session_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  source_id TEXT NOT NULL,
  snapshot_sha TEXT NOT NULL,
  source_path TEXT NOT NULL,
  document_commit_sha TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  content TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (session_id, sequence)
)""",
    """CREATE TABLE ingestion_issues (
  issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  snapshot_sha TEXT NOT NULL,
  document_commit_sha TEXT,
  raw_sha256 TEXT NOT NULL,
  session_id TEXT,
  command_id TEXT,
  error_code TEXT NOT NULL,
  detail TEXT NOT NULL,
  blocking INTEGER NOT NULL CHECK (blocking IN (0, 1)),
  created_at TEXT NOT NULL,
  UNIQUE (source_id, source_path, error_code, raw_sha256)
)""",
    "CREATE INDEX idx_commands_state ON commands(state)",
    "CREATE INDEX idx_commands_session_seq ON commands(session_id, sequence)",
    "CREATE INDEX idx_sessions_created ON sessions(created_at, session_id)",
    "CREATE INDEX idx_ingestion_issues_blocking ON ingestion_issues(blocking) WHERE blocking = 1",
    """CREATE UNIQUE INDEX idx_sessions_one_active
ON sessions((1))
WHERE state IN ('active', 'completing')""",
    """CREATE UNIQUE INDEX idx_commands_one_worker
ON commands((1))
WHERE state IN ('claimed', 'executing', 'effect_recorded')""",
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "journal_v1_initial", MIGRATION_V1_STATEMENTS),
    Migration(2, "journal_v2_ingestion", MIGRATION_V2_STATEMENTS),
)


def _validate_migration_registry(migrations: tuple[Migration, ...]) -> None:
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate migration versions in registry")
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError("migration versions must be contiguous from 1")


_validate_migration_registry(MIGRATIONS)


def _migration_by_version(migrations: tuple[Migration, ...]) -> dict[int, Migration]:
    return {migration.version: migration for migration in migrations}


def _list_user_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _read_applied_migrations(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]


def map_sqlite_error(exc: sqlite3.Error, *, context: str) -> BridgeError:
    message = str(exc).lower()
    if "database is locked" in message or "database is busy" in message:
        return BridgeError(
            BridgeErrorCode.JOURNAL_CONFLICT,
            f"SQLite database is locked during {context}",
        )
    return BridgeError(
        BridgeErrorCode.JOURNAL_CORRUPT,
        f"SQLite error during {context}: {exc}",
    )


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    *,
    now_fn: Callable[[], str] | None = None,
) -> None:
    _validate_migration_registry(migrations)
    migration_map = _migration_by_version(migrations)
    now = (now_fn or utc_now_iso)()

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="migration begin") from exc

    try:
        user_tables = _list_user_tables(conn)
        has_schema_migrations = "schema_migrations" in user_tables
        unknown_tables = user_tables - JOURNAL_TABLES

        if not has_schema_migrations and unknown_tables:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Database contains unknown tables without schema_migrations",
            )

        applied_rows: list[tuple[int, str, str]] = []
        if has_schema_migrations:
            applied_rows = _read_applied_migrations(conn)

        if applied_rows:
            versions = [row[0] for row in applied_rows]
            if versions != list(range(1, len(versions) + 1)):
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH,
                    "Applied migration versions contain gaps",
                )

            max_applied = versions[-1]
            if max_applied > len(migrations):
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_SCHEMA_UNSUPPORTED,
                    f"Database schema version {max_applied} is newer than supported {len(migrations)}",
                )

            for version, name, checksum in applied_rows:
                expected = migration_map.get(version)
                if expected is None:
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_SCHEMA_UNSUPPORTED,
                        f"Database schema version {version} is unsupported",
                    )
                if expected.name != name:
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH,
                        f"Migration name mismatch for version {version}",
                    )
                if expected.checksum() != checksum:
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH,
                        f"Migration checksum mismatch for version {version}",
                    )

        next_version = len(applied_rows) + 1
        while next_version <= len(migrations):
            migration = migration_map[next_version]
            if migration.version == 2:
                # Check for multiple active sessions in existing database
                cur = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
                )
                if cur.fetchone() is not None:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM sessions WHERE state IN ('active', 'completing')"
                    )
                    if cur.fetchone()[0] > 1:
                        raise BridgeError(
                            BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH,
                            "Cannot migrate to v2: multiple active or completing sessions exist in database",
                        )
                # Check for multiple claimed/executing/effect_recorded commands
                cur = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'commands'"
                )
                if cur.fetchone() is not None:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM commands WHERE state IN ('claimed', 'executing', 'effect_recorded')"
                    )
                    if cur.fetchone()[0] > 1:
                        raise BridgeError(
                            BridgeErrorCode.JOURNAL_MIGRATION_MISMATCH,
                            "Cannot migrate to v2: multiple active workers exist in database",
                        )

            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum(), now),
            )
            next_version += 1

        conn.commit()
    except BridgeError:
        _safe_rollback(conn)
        raise
    except sqlite3.Error as exc:
        _safe_rollback(conn)
        raise map_sqlite_error(exc, context="migration") from exc
    except Exception:
        _safe_rollback(conn)
        raise
    finally:
        if conn.in_transaction:
            _safe_rollback(conn)
