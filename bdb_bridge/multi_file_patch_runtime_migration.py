from __future__ import annotations

from typing import Type

from . import migrations as _base
from .serializers import MAX_TAIL_CHARS


MIGRATION_V10_STATEMENTS: tuple[str, ...] = (
    f"""CREATE TABLE multi_file_patch_profile_runs (
  command_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL CHECK (length(profile_id) > 0 AND length(profile_id) <= 80),
  status TEXT NOT NULL CHECK (status IN ('success','failed','timeout','internal_error')),
  exit_code INTEGER,
  stdout_tail TEXT NOT NULL CHECK (length(stdout_tail) <= {MAX_TAIL_CHARS}),
  stderr_tail TEXT NOT NULL CHECK (length(stderr_tail) <= {MAX_TAIL_CHARS}),
  stdout_sha256 TEXT NOT NULL CHECK (
    length(stdout_sha256) = 71 AND substr(stdout_sha256, 1, 7) = 'sha256:'
    AND substr(stdout_sha256, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  stderr_sha256 TEXT NOT NULL CHECK (
    length(stderr_sha256) = 71 AND substr(stderr_sha256, 1, 7) = 'sha256:'
    AND substr(stderr_sha256, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (command_id) REFERENCES multi_file_patch_checkpoints(command_id)
)""",
    """CREATE TRIGGER multi_file_patch_profile_runs_no_update
BEFORE UPDATE ON multi_file_patch_profile_runs
BEGIN
  SELECT RAISE(ABORT, 'multi-file profile runs are immutable');
END""",
    """CREATE TRIGGER multi_file_patch_profile_runs_no_delete
BEFORE DELETE ON multi_file_patch_profile_runs
BEGIN
  SELECT RAISE(ABORT, 'multi-file profile runs are durable');
END""",
)

MIGRATION_V10 = _base.Migration(
    10,
    "journal_v10_multi_file_patch_runtime",
    MIGRATION_V10_STATEMENTS,
)


def install_multi_file_patch_runtime_migration(journal_cls: Type[object]) -> None:
    if any(migration.version == 10 for migration in _base.MIGRATIONS):
        return
    _base.MIGRATIONS = (*_base.MIGRATIONS, MIGRATION_V10)
    _base.JOURNAL_TABLES = frozenset(
        (*_base.JOURNAL_TABLES, "multi_file_patch_profile_runs")
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
