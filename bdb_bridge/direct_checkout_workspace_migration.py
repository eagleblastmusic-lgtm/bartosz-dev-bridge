from __future__ import annotations

from typing import Type

from . import migrations as _base


MIGRATION_V11_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS workspace_lifecycle_validate_workspace_update",
    "DROP TRIGGER IF EXISTS workspace_lifecycle_sync_workspace_update",
    """CREATE TABLE workspaces_v11 (
  session_id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  base_sha TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision >= 0),
  state_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
)""",
    """INSERT INTO workspaces_v11 (
  session_id, workspace_path, base_sha, revision, state_hash, created_at, updated_at
)
SELECT
  session_id, workspace_path, base_sha, revision, state_hash, created_at, updated_at
FROM workspaces""",
    "DROP TABLE workspaces",
    "ALTER TABLE workspaces_v11 RENAME TO workspaces",
    """CREATE TABLE workspace_lifecycle_v11 (
  session_id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  base_sha TEXT NOT NULL CHECK (
    length(base_sha) = 40 AND base_sha NOT GLOB '*[^0-9a-fA-F]*'
  ),
  expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
  expected_state_hash TEXT NOT NULL CHECK (
    length(expected_state_hash) = 71
    AND substr(expected_state_hash, 1, 7) = 'sha256:'
    AND substr(expected_state_hash, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  disposition TEXT NOT NULL CHECK (disposition IN ('preserve', 'cleanup')),
  state TEXT NOT NULL CHECK (
    state IN ('preserved', 'cleanup_requested', 'removing', 'removed', 'blocked')
  ),
  requested_at TEXT CHECK (
    requested_at IS NULL OR (
      length(requested_at) >= 20 AND substr(requested_at, -1) = 'Z'
    )
  ),
  started_at TEXT CHECK (
    started_at IS NULL OR (
      length(started_at) >= 20 AND substr(started_at, -1) = 'Z'
    )
  ),
  completed_at TEXT CHECK (
    completed_at IS NULL OR (
      length(completed_at) >= 20 AND substr(completed_at, -1) = 'Z'
    )
  ),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 500),
  created_at TEXT NOT NULL CHECK (
    length(created_at) >= 20 AND substr(created_at, -1) = 'Z'
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at) >= 20 AND substr(updated_at, -1) = 'Z'
  ),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
)""",
    """INSERT INTO workspace_lifecycle_v11 (
  session_id, workspace_path, base_sha, expected_revision, expected_state_hash,
  disposition, state, requested_at, started_at, completed_at, last_error,
  created_at, updated_at
)
SELECT
  session_id, workspace_path, base_sha, expected_revision, expected_state_hash,
  disposition, state, requested_at, started_at, completed_at, last_error,
  created_at, updated_at
FROM workspace_lifecycle""",
    "DROP TABLE workspace_lifecycle",
    "ALTER TABLE workspace_lifecycle_v11 RENAME TO workspace_lifecycle",
    "CREATE INDEX idx_workspace_lifecycle_state ON workspace_lifecycle(state, updated_at, session_id)",
    """CREATE TRIGGER workspace_lifecycle_validate_workspace_update
BEFORE UPDATE OF revision, state_hash ON workspaces
WHEN EXISTS (
  SELECT 1 FROM workspace_lifecycle
  WHERE session_id = OLD.session_id
)
AND NOT EXISTS (
  SELECT 1 FROM workspace_lifecycle
  WHERE session_id = OLD.session_id
    AND workspace_path = OLD.workspace_path
    AND base_sha = OLD.base_sha
    AND expected_revision = OLD.revision
    AND expected_state_hash = OLD.state_hash
    AND disposition = 'preserve'
    AND state = 'preserved'
)
BEGIN
  SELECT RAISE(ABORT, 'workspace lifecycle identity conflict');
END""",
    """CREATE TRIGGER workspace_lifecycle_sync_workspace_update
AFTER UPDATE OF revision, state_hash ON workspaces
WHEN EXISTS (
  SELECT 1 FROM workspace_lifecycle
  WHERE session_id = OLD.session_id
    AND workspace_path = OLD.workspace_path
    AND base_sha = OLD.base_sha
    AND expected_revision = OLD.revision
    AND expected_state_hash = OLD.state_hash
    AND disposition = 'preserve'
    AND state = 'preserved'
)
BEGIN
  UPDATE workspace_lifecycle
  SET expected_revision = NEW.revision,
      expected_state_hash = NEW.state_hash,
      updated_at = NEW.updated_at
  WHERE session_id = NEW.session_id;
END""",
)

MIGRATION_V11 = _base.Migration(
    11,
    "journal_v11_shared_direct_checkout_paths",
    MIGRATION_V11_STATEMENTS,
)


def install_direct_checkout_workspace_migration(journal_cls: Type[object]) -> None:
    if any(migration.version == 11 for migration in _base.MIGRATIONS):
        return

    _base.MIGRATIONS = (*_base.MIGRATIONS, MIGRATION_V11)
    _base._validate_migration_registry(_base.MIGRATIONS)
    if _base.apply_migrations.__kwdefaults__ is not None:
        _base.apply_migrations.__kwdefaults__["migrations"] = _base.MIGRATIONS

    def migrate(self: object) -> None:
        from . import journal as _journal

        self._ensure_open()
        _journal.apply_migrations(
            self._conn,
            migrations=_base.MIGRATIONS,
            now_fn=self._now_fn,
        )

    setattr(journal_cls, "migrate", migrate)
