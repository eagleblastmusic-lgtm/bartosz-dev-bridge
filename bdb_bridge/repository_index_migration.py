from __future__ import annotations

from typing import Type

from . import migrations as _base


MIGRATION_V7_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE repository_snapshots (
  repository_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL CHECK (
    length(commit_sha) = 40 AND commit_sha NOT GLOB '*[^0-9a-f]*'
  ),
  tree_sha TEXT NOT NULL CHECK (
    length(tree_sha) = 40 AND tree_sha NOT GLOB '*[^0-9a-f]*'
  ),
  indexed_at TEXT NOT NULL CHECK (
    length(indexed_at) >= 20 AND substr(indexed_at, -1) = 'Z'
  ),
  file_count INTEGER NOT NULL CHECK (file_count >= 0),
  text_file_count INTEGER NOT NULL CHECK (text_file_count >= 0),
  binary_file_count INTEGER NOT NULL CHECK (binary_file_count >= 0),
  python_file_count INTEGER NOT NULL CHECK (python_file_count >= 0),
  symbol_count INTEGER NOT NULL CHECK (symbol_count >= 0),
  indexer_version TEXT NOT NULL CHECK (length(indexer_version) > 0 AND length(indexer_version) <= 64),
  PRIMARY KEY (repository_id, commit_sha)
)""",
    "CREATE INDEX idx_repository_snapshots_indexed_at ON repository_snapshots(indexed_at, repository_id, commit_sha)",
    """CREATE TABLE repository_files (
  repository_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL CHECK (
    length(commit_sha) = 40 AND commit_sha NOT GLOB '*[^0-9a-f]*'
  ),
  path TEXT NOT NULL CHECK (length(path) > 0 AND length(path) <= 1024),
  git_mode TEXT NOT NULL CHECK (length(git_mode) > 0 AND length(git_mode) <= 16),
  git_object_type TEXT NOT NULL CHECK (git_object_type IN ('blob', 'commit', 'tree')),
  object_sha TEXT NOT NULL CHECK (
    length(object_sha) = 40 AND object_sha NOT GLOB '*[^0-9a-f]*'
  ),
  size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
  content_sha256 TEXT NOT NULL CHECK (
    length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  file_kind TEXT NOT NULL CHECK (file_kind IN ('regular', 'symlink', 'submodule')),
  language TEXT NOT NULL CHECK (length(language) > 0 AND length(language) <= 32),
  is_text INTEGER NOT NULL CHECK (is_text IN (0, 1)),
  line_count INTEGER CHECK (line_count IS NULL OR line_count >= 0),
  parse_status TEXT NOT NULL CHECK (
    parse_status IN (
      'ok', 'unsupported_language', 'syntax_error', 'too_large', 'binary', 'metadata_only'
    )
  ),
  parse_diagnostic TEXT CHECK (parse_diagnostic IS NULL OR length(parse_diagnostic) <= 500),
  PRIMARY KEY (repository_id, commit_sha, path),
  FOREIGN KEY (repository_id, commit_sha)
    REFERENCES repository_snapshots(repository_id, commit_sha)
)""",
    "CREATE INDEX idx_repository_files_language ON repository_files(repository_id, commit_sha, language, path)",
    "CREATE INDEX idx_repository_files_parse_status ON repository_files(repository_id, commit_sha, parse_status, path)",
    """CREATE TABLE repository_symbols (
  repository_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL CHECK (
    length(commit_sha) = 40 AND commit_sha NOT GLOB '*[^0-9a-f]*'
  ),
  path TEXT NOT NULL CHECK (length(path) > 0 AND length(path) <= 1024),
  symbol_id TEXT NOT NULL CHECK (length(symbol_id) = 64 AND symbol_id NOT GLOB '*[^0-9a-f]*'),
  parent_symbol_id TEXT CHECK (
    parent_symbol_id IS NULL
    OR (length(parent_symbol_id) = 64 AND parent_symbol_id NOT GLOB '*[^0-9a-f]*')
  ),
  kind TEXT NOT NULL CHECK (
    kind IN (
      'class', 'function', 'async_function', 'method', 'async_method',
      'nested_function', 'nested_class'
    )
  ),
  name TEXT NOT NULL CHECK (length(name) > 0 AND length(name) <= 256),
  qualified_name TEXT NOT NULL CHECK (length(qualified_name) > 0 AND length(qualified_name) <= 1024),
  start_line INTEGER NOT NULL CHECK (start_line >= 1),
  end_line INTEGER NOT NULL CHECK (end_line >= 1),
  start_column INTEGER NOT NULL CHECK (start_column >= 0),
  end_column INTEGER NOT NULL CHECK (end_column >= 0),
  signature TEXT CHECK (signature IS NULL OR length(signature) <= 2048),
  decorators_json TEXT NOT NULL CHECK (length(decorators_json) >= 2 AND length(decorators_json) <= 4096),
  docstring_summary TEXT CHECK (docstring_summary IS NULL OR length(docstring_summary) <= 500),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (repository_id, commit_sha, symbol_id),
  FOREIGN KEY (repository_id, commit_sha, path)
    REFERENCES repository_files(repository_id, commit_sha, path),
  UNIQUE (repository_id, commit_sha, path, ordinal)
)""",
    "CREATE INDEX idx_repository_symbols_path ON repository_symbols(repository_id, commit_sha, path, ordinal)",
    "CREATE INDEX idx_repository_symbols_qualified ON repository_symbols(repository_id, commit_sha, qualified_name)",
)


MIGRATION_V7 = _base.Migration(
    7,
    "journal_v7_repository_index",
    MIGRATION_V7_STATEMENTS,
)


def install_repository_index_migration(journal_cls: Type[object]) -> None:
    if any(m.version == 7 for m in _base.MIGRATIONS):
        return

    _base.MIGRATIONS = (*_base.MIGRATIONS, MIGRATION_V7)
    _base.JOURNAL_TABLES = frozenset(
        (
            *_base.JOURNAL_TABLES,
            "repository_snapshots",
            "repository_files",
            "repository_symbols",
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
