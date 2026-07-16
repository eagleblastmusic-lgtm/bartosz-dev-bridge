from __future__ import annotations

from typing import Type

from . import migrations as _base


MIGRATION_V8_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE repository_analyses (
  repository_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL CHECK (
    length(commit_sha) = 40 AND commit_sha NOT GLOB '*[^0-9a-f]*'
  ),
  analysis_version TEXT NOT NULL CHECK (
    length(analysis_version) > 0 AND length(analysis_version) <= 64
  ),
  analyzed_at TEXT NOT NULL CHECK (
    length(analyzed_at) >= 20 AND substr(analyzed_at, -1) = 'Z'
  ),
  python_file_count INTEGER NOT NULL CHECK (python_file_count >= 0),
  import_count INTEGER NOT NULL CHECK (import_count >= 0),
  reference_count INTEGER NOT NULL CHECK (reference_count >= 0),
  resolved_reference_count INTEGER NOT NULL CHECK (resolved_reference_count >= 0),
  call_edge_count INTEGER NOT NULL CHECK (call_edge_count >= 0),
  dependency_edge_count INTEGER NOT NULL CHECK (dependency_edge_count >= 0),
  PRIMARY KEY (repository_id, commit_sha, analysis_version),
  FOREIGN KEY (repository_id, commit_sha)
    REFERENCES repository_snapshots(repository_id, commit_sha)
)""",
    "CREATE INDEX idx_repository_analyses_time ON repository_analyses(analyzed_at, repository_id, commit_sha)",
    """CREATE TABLE repository_imports (
  repository_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  analysis_version TEXT NOT NULL,
  import_id TEXT NOT NULL CHECK (
    length(import_id) = 64 AND import_id NOT GLOB '*[^0-9a-f]*'
  ),
  source_path TEXT NOT NULL CHECK (length(source_path) > 0 AND length(source_path) <= 1024),
  source_symbol_id TEXT,
  import_kind TEXT NOT NULL CHECK (import_kind IN ('import', 'from_import')),
  module_name TEXT NOT NULL CHECK (length(module_name) <= 1024),
  imported_name TEXT CHECK (imported_name IS NULL OR length(imported_name) <= 512),
  alias TEXT CHECK (alias IS NULL OR length(alias) <= 512),
  relative_level INTEGER NOT NULL CHECK (relative_level >= 0),
  start_line INTEGER NOT NULL CHECK (start_line >= 1),
  start_column INTEGER NOT NULL CHECK (start_column >= 0),
  resolved_path TEXT CHECK (resolved_path IS NULL OR length(resolved_path) <= 1024),
  resolved_symbol_id TEXT,
  resolution_status TEXT NOT NULL CHECK (
    resolution_status IN ('resolved','unresolved','ambiguous','external','dynamic','unsupported')
  ),
  confidence TEXT NOT NULL CHECK (confidence IN ('exact','high','heuristic','none')),
  diagnostic TEXT CHECK (diagnostic IS NULL OR length(diagnostic) <= 500),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (repository_id, commit_sha, analysis_version, import_id),
  FOREIGN KEY (repository_id, commit_sha, analysis_version)
    REFERENCES repository_analyses(repository_id, commit_sha, analysis_version),
  FOREIGN KEY (repository_id, commit_sha, source_path)
    REFERENCES repository_files(repository_id, commit_sha, path),
  FOREIGN KEY (repository_id, commit_sha, source_symbol_id)
    REFERENCES repository_symbols(repository_id, commit_sha, symbol_id),
  FOREIGN KEY (repository_id, commit_sha, resolved_symbol_id)
    REFERENCES repository_symbols(repository_id, commit_sha, symbol_id),
  UNIQUE (repository_id, commit_sha, analysis_version, source_path, ordinal)
)""",
    "CREATE INDEX idx_repository_imports_source ON repository_imports(repository_id, commit_sha, analysis_version, source_path, ordinal)",
    "CREATE INDEX idx_repository_imports_target ON repository_imports(repository_id, commit_sha, analysis_version, resolved_path)",
    """CREATE TABLE repository_symbol_references (
  repository_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  analysis_version TEXT NOT NULL,
  reference_id TEXT NOT NULL CHECK (
    length(reference_id) = 64 AND reference_id NOT GLOB '*[^0-9a-f]*'
  ),
  source_path TEXT NOT NULL CHECK (length(source_path) > 0 AND length(source_path) <= 1024),
  source_symbol_id TEXT,
  target_path TEXT CHECK (target_path IS NULL OR length(target_path) <= 1024),
  target_symbol_id TEXT,
  reference_kind TEXT NOT NULL CHECK (
    reference_kind IN ('call','name_read','attribute_read','decorator','base_class','annotation')
  ),
  expression TEXT NOT NULL CHECK (length(expression) > 0 AND length(expression) <= 1024),
  start_line INTEGER NOT NULL CHECK (start_line >= 1),
  end_line INTEGER NOT NULL CHECK (end_line >= 1),
  start_column INTEGER NOT NULL CHECK (start_column >= 0),
  end_column INTEGER NOT NULL CHECK (end_column >= 0),
  resolution_status TEXT NOT NULL CHECK (
    resolution_status IN ('resolved','unresolved','ambiguous','external','dynamic','unsupported')
  ),
  confidence TEXT NOT NULL CHECK (confidence IN ('exact','high','heuristic','none')),
  diagnostic TEXT CHECK (diagnostic IS NULL OR length(diagnostic) <= 500),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (repository_id, commit_sha, analysis_version, reference_id),
  FOREIGN KEY (repository_id, commit_sha, analysis_version)
    REFERENCES repository_analyses(repository_id, commit_sha, analysis_version),
  FOREIGN KEY (repository_id, commit_sha, source_path)
    REFERENCES repository_files(repository_id, commit_sha, path),
  FOREIGN KEY (repository_id, commit_sha, source_symbol_id)
    REFERENCES repository_symbols(repository_id, commit_sha, symbol_id),
  FOREIGN KEY (repository_id, commit_sha, target_symbol_id)
    REFERENCES repository_symbols(repository_id, commit_sha, symbol_id),
  UNIQUE (repository_id, commit_sha, analysis_version, source_path, ordinal)
)""",
    "CREATE INDEX idx_repository_references_source ON repository_symbol_references(repository_id, commit_sha, analysis_version, source_symbol_id, ordinal)",
    "CREATE INDEX idx_repository_references_target ON repository_symbol_references(repository_id, commit_sha, analysis_version, target_symbol_id, reference_kind)",
    """CREATE TABLE repository_dependency_edges (
  repository_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  analysis_version TEXT NOT NULL,
  edge_id TEXT NOT NULL CHECK (
    length(edge_id) = 64 AND edge_id NOT GLOB '*[^0-9a-f]*'
  ),
  source_path TEXT NOT NULL CHECK (length(source_path) > 0 AND length(source_path) <= 1024),
  source_symbol_id TEXT,
  target_path TEXT NOT NULL CHECK (length(target_path) > 0 AND length(target_path) <= 1024),
  target_symbol_id TEXT,
  edge_kind TEXT NOT NULL CHECK (edge_kind IN ('import','call','reference')),
  resolution_status TEXT NOT NULL CHECK (resolution_status = 'resolved'),
  confidence TEXT NOT NULL CHECK (confidence IN ('exact','high','heuristic')),
  origin_reference_id TEXT,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (repository_id, commit_sha, analysis_version, edge_id),
  FOREIGN KEY (repository_id, commit_sha, analysis_version)
    REFERENCES repository_analyses(repository_id, commit_sha, analysis_version),
  FOREIGN KEY (repository_id, commit_sha, source_path)
    REFERENCES repository_files(repository_id, commit_sha, path),
  FOREIGN KEY (repository_id, commit_sha, target_path)
    REFERENCES repository_files(repository_id, commit_sha, path),
  FOREIGN KEY (repository_id, commit_sha, source_symbol_id)
    REFERENCES repository_symbols(repository_id, commit_sha, symbol_id),
  FOREIGN KEY (repository_id, commit_sha, target_symbol_id)
    REFERENCES repository_symbols(repository_id, commit_sha, symbol_id),
  UNIQUE (repository_id, commit_sha, analysis_version, ordinal)
)""",
    "CREATE INDEX idx_repository_edges_source ON repository_dependency_edges(repository_id, commit_sha, analysis_version, source_path, edge_kind, ordinal)",
    "CREATE INDEX idx_repository_edges_target ON repository_dependency_edges(repository_id, commit_sha, analysis_version, target_path, edge_kind, ordinal)",
)


MIGRATION_V8 = _base.Migration(
    8,
    "journal_v8_code_relationships",
    MIGRATION_V8_STATEMENTS,
)


def install_code_relationship_migration(journal_cls: Type[object]) -> None:
    if any(m.version == 8 for m in _base.MIGRATIONS):
        return

    _base.MIGRATIONS = (*_base.MIGRATIONS, MIGRATION_V8)
    _base.JOURNAL_TABLES = frozenset(
        (
            *_base.JOURNAL_TABLES,
            "repository_analyses",
            "repository_imports",
            "repository_symbol_references",
            "repository_dependency_edges",
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
