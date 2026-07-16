from __future__ import annotations

from typing import Type

from . import migrations as _base


MIGRATION_V9_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE multi_file_patch_checkpoints (
  command_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  patch_sha256 TEXT NOT NULL CHECK (
    length(patch_sha256) = 71 AND substr(patch_sha256, 1, 7) = 'sha256:'
    AND substr(patch_sha256, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  plan_sha256 TEXT NOT NULL CHECK (
    length(plan_sha256) = 71 AND substr(plan_sha256, 1, 7) = 'sha256:'
    AND substr(plan_sha256, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  checkpoint_sha256 TEXT NOT NULL CHECK (
    length(checkpoint_sha256) = 71 AND substr(checkpoint_sha256, 1, 7) = 'sha256:'
    AND substr(checkpoint_sha256, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  state TEXT NOT NULL CHECK (
    state IN ('planned','applying','applied','rolling_back','rolled_back','committed','blocked')
  ),
  workspace_revision_before INTEGER NOT NULL CHECK (workspace_revision_before >= 0),
  workspace_state_hash_before TEXT NOT NULL CHECK (
    length(workspace_state_hash_before) = 71
    AND substr(workspace_state_hash_before, 1, 7) = 'sha256:'
    AND substr(workspace_state_hash_before, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  workspace_revision_after INTEGER,
  workspace_state_hash_after TEXT NOT NULL CHECK (
    length(workspace_state_hash_after) = 71
    AND substr(workspace_state_hash_after, 1, 7) = 'sha256:'
    AND substr(workspace_state_hash_after, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  path_count INTEGER NOT NULL CHECK (path_count > 0 AND path_count <= 200),
  total_before_bytes INTEGER NOT NULL CHECK (total_before_bytes >= 0),
  total_after_bytes INTEGER NOT NULL CHECK (total_after_bytes >= 0),
  last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 500),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (state = 'committed' AND workspace_revision_after = workspace_revision_before + 1)
    OR (state != 'committed' AND workspace_revision_after IS NULL)
  ),
  FOREIGN KEY (command_id) REFERENCES commands(command_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
)""",
    "CREATE INDEX idx_multi_file_patch_checkpoint_state ON multi_file_patch_checkpoints(state, updated_at, command_id)",
    """CREATE TABLE multi_file_patch_checkpoint_paths (
  command_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  path TEXT NOT NULL CHECK (length(path) > 0 AND length(path) <= 1024),
  before_exists INTEGER NOT NULL CHECK (before_exists IN (0, 1)),
  before_content BLOB,
  before_sha256 TEXT,
  after_exists INTEGER NOT NULL CHECK (after_exists IN (0, 1)),
  after_content BLOB,
  after_sha256 TEXT,
  roles_json TEXT NOT NULL CHECK (length(roles_json) > 0 AND length(roles_json) <= 2000),
  operation_indices_json TEXT NOT NULL CHECK (
    length(operation_indices_json) > 0 AND length(operation_indices_json) <= 2000
  ),
  PRIMARY KEY (command_id, path),
  UNIQUE (command_id, ordinal),
  CHECK (
    (before_exists = 1 AND before_content IS NOT NULL AND before_sha256 IS NOT NULL)
    OR (before_exists = 0 AND before_content IS NULL AND before_sha256 IS NULL)
  ),
  CHECK (
    (after_exists = 1 AND after_content IS NOT NULL AND after_sha256 IS NOT NULL)
    OR (after_exists = 0 AND after_content IS NULL AND after_sha256 IS NULL)
  ),
  FOREIGN KEY (command_id) REFERENCES multi_file_patch_checkpoints(command_id)
)""",
    "CREATE INDEX idx_multi_file_patch_paths_ordinal ON multi_file_patch_checkpoint_paths(command_id, ordinal)",
    """CREATE TRIGGER multi_file_patch_paths_no_update
BEFORE UPDATE ON multi_file_patch_checkpoint_paths
BEGIN
  SELECT RAISE(ABORT, 'multi-file checkpoint paths are immutable');
END""",
    """CREATE TRIGGER multi_file_patch_paths_no_delete
BEFORE DELETE ON multi_file_patch_checkpoint_paths
BEGIN
  SELECT RAISE(ABORT, 'multi-file checkpoint paths are immutable');
END""",
)

MIGRATION_V9 = _base.Migration(
    9,
    "journal_v9_multi_file_patch_recovery",
    MIGRATION_V9_STATEMENTS,
)


def install_multi_file_patch_migration(journal_cls: Type[object]) -> None:
    if any(m.version == 9 for m in _base.MIGRATIONS):
        return

    _base.MIGRATIONS = (*_base.MIGRATIONS, MIGRATION_V9)
    _base.JOURNAL_TABLES = frozenset(
        (
            *_base.JOURNAL_TABLES,
            "multi_file_patch_checkpoints",
            "multi_file_patch_checkpoint_paths",
        )
    )
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
