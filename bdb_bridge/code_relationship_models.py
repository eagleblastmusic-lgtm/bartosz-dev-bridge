from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

ANALYSIS_VERSION = "ghb1b-v1"
MAX_QUERY_LENGTH = 200
MAX_SEARCH_LIMIT = 200
MAX_GRAPH_DEPTH = 10
MAX_GRAPH_NODES = 1000


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    EXTERNAL = "external"
    DYNAMIC = "dynamic"
    UNSUPPORTED = "unsupported"


class Confidence(str, Enum):
    EXACT = "exact"
    HIGH = "high"
    HEURISTIC = "heuristic"
    NONE = "none"


class ImportKind(str, Enum):
    IMPORT = "import"
    FROM_IMPORT = "from_import"


class ReferenceKind(str, Enum):
    CALL = "call"
    NAME_READ = "name_read"
    ATTRIBUTE_READ = "attribute_read"
    DECORATOR = "decorator"
    BASE_CLASS = "base_class"
    ANNOTATION = "annotation"


class EdgeKind(str, Enum):
    IMPORT = "import"
    CALL = "call"
    REFERENCE = "reference"


@dataclass(frozen=True)
class AnalysisImport:
    import_id: str
    source_path: str
    source_symbol_id: str | None
    import_kind: ImportKind
    module_name: str
    imported_name: str | None
    alias: str | None
    relative_level: int
    start_line: int
    start_column: int
    resolved_path: str | None
    resolved_symbol_id: str | None
    resolution_status: ResolutionStatus
    confidence: Confidence
    diagnostic: str | None
    ordinal: int


@dataclass(frozen=True)
class SymbolReference:
    reference_id: str
    source_path: str
    source_symbol_id: str | None
    target_path: str | None
    target_symbol_id: str | None
    reference_kind: ReferenceKind
    expression: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    resolution_status: ResolutionStatus
    confidence: Confidence
    diagnostic: str | None
    ordinal: int


@dataclass(frozen=True)
class DependencyEdge:
    edge_id: str
    source_path: str
    source_symbol_id: str | None
    target_path: str
    target_symbol_id: str | None
    edge_kind: EdgeKind
    resolution_status: ResolutionStatus
    confidence: Confidence
    origin_reference_id: str | None
    ordinal: int


@dataclass(frozen=True)
class RepositoryAnalysis:
    repository_id: str
    commit_sha: str
    analysis_version: str
    analyzed_at: str
    python_file_count: int
    import_count: int
    reference_count: int
    resolved_reference_count: int
    call_edge_count: int
    dependency_edge_count: int
    imports: tuple[AnalysisImport, ...] = ()
    references: tuple[SymbolReference, ...] = ()
    edges: tuple[DependencyEdge, ...] = ()


@dataclass(frozen=True)
class AnalysisPersistOutcome:
    analysis: RepositoryAnalysis
    created: bool
    idempotent: bool


@dataclass(frozen=True)
class SearchResult:
    result_kind: str
    path: str
    symbol_id: str | None
    symbol_kind: str | None
    name: str
    qualified_name: str | None
    start_line: int | None
    signature: str | None
    docstring_summary: str | None
    match_field: str
    rank: int
