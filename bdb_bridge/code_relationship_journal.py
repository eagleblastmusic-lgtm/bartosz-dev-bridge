from __future__ import annotations

import sqlite3
from typing import Type

from .code_relationship_models import (
    ANALYSIS_VERSION,
    AnalysisImport,
    AnalysisPersistOutcome,
    Confidence,
    DependencyEdge,
    EdgeKind,
    ImportKind,
    ReferenceKind,
    RepositoryAnalysis,
    ResolutionStatus,
    SymbolReference,
)
from .migrations import map_sqlite_error
from .protocol import BridgeError, parse_strict_utc_timestamp, validate_repo_relative_path


_ANALYSIS_SELECT = """
SELECT repository_id, commit_sha, analysis_version, analyzed_at, python_file_count,
       import_count, reference_count, resolved_reference_count, call_edge_count,
       dependency_edge_count
FROM repository_analyses
WHERE repository_id = ? AND commit_sha = ? AND analysis_version = ?
"""

_IMPORT_SELECT = """
SELECT import_id, source_path, source_symbol_id, import_kind, module_name, imported_name,
       alias, relative_level, start_line, start_column, resolved_path, resolved_symbol_id,
       resolution_status, confidence, diagnostic, ordinal
FROM repository_imports
WHERE repository_id = ? AND commit_sha = ? AND analysis_version = ?
ORDER BY ordinal ASC
"""

_REFERENCE_SELECT = """
SELECT reference_id, source_path, source_symbol_id, target_path, target_symbol_id,
       reference_kind, expression, start_line, end_line, start_column, end_column,
       resolution_status, confidence, diagnostic, ordinal
FROM repository_symbol_references
WHERE repository_id = ? AND commit_sha = ? AND analysis_version = ?
ORDER BY ordinal ASC
"""

_EDGE_SELECT = """
SELECT edge_id, source_path, source_symbol_id, target_path, target_symbol_id, edge_kind,
       resolution_status, confidence, origin_reference_id, ordinal
FROM repository_dependency_edges
WHERE repository_id = ? AND commit_sha = ? AND analysis_version = ?
ORDER BY ordinal ASC
"""


def _sha(value: str) -> str:
    if not isinstance(value, str):
        raise BridgeError("invalid_payload", "commit_sha must be a string")
    value = value.lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise BridgeError("invalid_payload", "commit_sha must be lowercase 40-character hex")
    return value


def _analysis_row(row: tuple[object, ...]) -> RepositoryAnalysis:
    try:
        result = RepositoryAnalysis(
            repository_id=str(row[0]), commit_sha=_sha(str(row[1])),
            analysis_version=str(row[2]), analyzed_at=str(row[3]),
            python_file_count=int(row[4]), import_count=int(row[5]),
            reference_count=int(row[6]), resolved_reference_count=int(row[7]),
            call_edge_count=int(row[8]), dependency_edge_count=int(row[9]),
        )
        parse_strict_utc_timestamp(result.analyzed_at, field="analyzed_at")
        counts = (result.python_file_count, result.import_count, result.reference_count,
                  result.resolved_reference_count, result.call_edge_count, result.dependency_edge_count)
        if not result.repository_id or not result.analysis_version or any(item < 0 for item in counts):
            raise ValueError("invalid identity or counts")
        if result.resolved_reference_count > result.reference_count:
            raise ValueError("resolved references exceed references")
        if result.call_edge_count > result.dependency_edge_count:
            raise ValueError("call edges exceed dependency edges")
        return result
    except (BridgeError, ValueError, TypeError) as exc:
        raise BridgeError("journal_corrupt", "Invalid repository_analyses row") from exc


def _import_row(row: tuple[object, ...]) -> AnalysisImport:
    try:
        item = AnalysisImport(
            import_id=str(row[0]), source_path=validate_repo_relative_path(str(row[1])),
            source_symbol_id=None if row[2] is None else str(row[2]), import_kind=ImportKind(str(row[3])),
            module_name=str(row[4]), imported_name=None if row[5] is None else str(row[5]),
            alias=None if row[6] is None else str(row[6]), relative_level=int(row[7]),
            start_line=int(row[8]), start_column=int(row[9]),
            resolved_path=None if row[10] is None else validate_repo_relative_path(str(row[10])),
            resolved_symbol_id=None if row[11] is None else str(row[11]),
            resolution_status=ResolutionStatus(str(row[12])), confidence=Confidence(str(row[13])),
            diagnostic=None if row[14] is None else str(row[14]), ordinal=int(row[15]),
        )
        _validate_id(item.import_id, "import_id")
        if item.relative_level < 0 or item.start_line < 1 or item.start_column < 0 or item.ordinal < 0:
            raise ValueError("invalid import range")
        return item
    except (BridgeError, ValueError, TypeError) as exc:
        raise BridgeError("journal_corrupt", "Invalid repository_imports row") from exc


def _reference_row(row: tuple[object, ...]) -> SymbolReference:
    try:
        item = SymbolReference(
            reference_id=str(row[0]), source_path=validate_repo_relative_path(str(row[1])),
            source_symbol_id=None if row[2] is None else str(row[2]),
            target_path=None if row[3] is None else validate_repo_relative_path(str(row[3])),
            target_symbol_id=None if row[4] is None else str(row[4]), reference_kind=ReferenceKind(str(row[5])),
            expression=str(row[6]), start_line=int(row[7]), end_line=int(row[8]),
            start_column=int(row[9]), end_column=int(row[10]),
            resolution_status=ResolutionStatus(str(row[11])), confidence=Confidence(str(row[12])),
            diagnostic=None if row[13] is None else str(row[13]), ordinal=int(row[14]),
        )
        _validate_id(item.reference_id, "reference_id")
        if (item.start_line < 1 or item.end_line < item.start_line or item.start_column < 0
                or item.end_column < 0 or item.ordinal < 0 or not item.expression):
            raise ValueError("invalid reference range")
        return item
    except (BridgeError, ValueError, TypeError) as exc:
        raise BridgeError("journal_corrupt", "Invalid repository_symbol_references row") from exc


def _edge_row(row: tuple[object, ...]) -> DependencyEdge:
    try:
        item = DependencyEdge(
            edge_id=str(row[0]), source_path=validate_repo_relative_path(str(row[1])),
            source_symbol_id=None if row[2] is None else str(row[2]),
            target_path=validate_repo_relative_path(str(row[3])),
            target_symbol_id=None if row[4] is None else str(row[4]), edge_kind=EdgeKind(str(row[5])),
            resolution_status=ResolutionStatus(str(row[6])), confidence=Confidence(str(row[7])),
            origin_reference_id=None if row[8] is None else str(row[8]), ordinal=int(row[9]),
        )
        _validate_id(item.edge_id, "edge_id")
        if item.resolution_status is not ResolutionStatus.RESOLVED or item.ordinal < 0:
            raise ValueError("invalid edge")
        return item
    except (BridgeError, ValueError, TypeError) as exc:
        raise BridgeError("journal_corrupt", "Invalid repository_dependency_edges row") from exc


def _validate_id(value: str, field: str) -> None:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"invalid {field}")


def get_repository_analysis(journal: object, repository_id: str, commit_sha: str,
                            analysis_version: str = ANALYSIS_VERSION, *,
                            include_records: bool = True) -> RepositoryAnalysis | None:
    journal._ensure_open(); commit_sha = _sha(commit_sha)
    if not repository_id or not analysis_version:
        raise BridgeError("invalid_payload", "repository_id and analysis_version are required")
    try:
        row = journal._connection.execute(_ANALYSIS_SELECT, (repository_id, commit_sha, analysis_version)).fetchone()
        if row is None: return None
        summary = _analysis_row(row)
        if not include_records: return summary
        imports = tuple(_import_row(item) for item in journal._connection.execute(_IMPORT_SELECT, (repository_id, commit_sha, analysis_version)).fetchall())
        references = tuple(_reference_row(item) for item in journal._connection.execute(_REFERENCE_SELECT, (repository_id, commit_sha, analysis_version)).fetchall())
        edges = tuple(_edge_row(item) for item in journal._connection.execute(_EDGE_SELECT, (repository_id, commit_sha, analysis_version)).fetchall())
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="repository analysis read") from exc
    result = RepositoryAnalysis(
        summary.repository_id, summary.commit_sha, summary.analysis_version, summary.analyzed_at,
        summary.python_file_count, summary.import_count, summary.reference_count,
        summary.resolved_reference_count, summary.call_edge_count, summary.dependency_edge_count,
        imports, references, edges,
    )
    _validate_counts(result, error_code="journal_corrupt")
    return result


def list_repository_imports(journal: object, repository_id: str, commit_sha: str,
                            analysis_version: str = ANALYSIS_VERSION) -> tuple[AnalysisImport, ...]:
    analysis = get_repository_analysis(journal, repository_id, commit_sha, analysis_version, include_records=True)
    return () if analysis is None else analysis.imports


def list_symbol_references(journal: object, repository_id: str, commit_sha: str,
                           analysis_version: str = ANALYSIS_VERSION) -> tuple[SymbolReference, ...]:
    analysis = get_repository_analysis(journal, repository_id, commit_sha, analysis_version, include_records=True)
    return () if analysis is None else analysis.references


def list_dependency_edges(journal: object, repository_id: str, commit_sha: str,
                          analysis_version: str = ANALYSIS_VERSION) -> tuple[DependencyEdge, ...]:
    analysis = get_repository_analysis(journal, repository_id, commit_sha, analysis_version, include_records=True)
    return () if analysis is None else analysis.edges


def save_repository_analysis(journal: object, analysis: RepositoryAnalysis) -> AnalysisPersistOutcome:
    journal._ensure_open(); commit_sha = _sha(analysis.commit_sha)
    parse_strict_utc_timestamp(analysis.analyzed_at, field="analyzed_at")
    _validate_counts(analysis, error_code="invalid_payload")
    existing = get_repository_analysis(journal, analysis.repository_id, commit_sha,
                                       analysis.analysis_version, include_records=True)
    if existing is not None:
        if _same_analysis(existing, analysis):
            return AnalysisPersistOutcome(existing, created=False, idempotent=True)
        raise BridgeError("analysis_conflict", "Different immutable analysis already exists")
    try:
        with journal._transaction():
            snapshot = journal._connection.execute(
                "SELECT 1 FROM repository_snapshots WHERE repository_id=? AND commit_sha=?",
                (analysis.repository_id, commit_sha),
            ).fetchone()
            if snapshot is None:
                raise BridgeError("snapshot_not_found", "Repository snapshot is required before analysis")
            journal._connection.execute(
                """INSERT INTO repository_analyses(
                    repository_id,commit_sha,analysis_version,analyzed_at,python_file_count,
                    import_count,reference_count,resolved_reference_count,call_edge_count,
                    dependency_edge_count) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (analysis.repository_id, commit_sha, analysis.analysis_version, analysis.analyzed_at,
                 analysis.python_file_count, analysis.import_count, analysis.reference_count,
                 analysis.resolved_reference_count, analysis.call_edge_count, analysis.dependency_edge_count),
            )
            for item in analysis.imports:
                journal._connection.execute(
                    """INSERT INTO repository_imports(
                        repository_id,commit_sha,analysis_version,import_id,source_path,source_symbol_id,
                        import_kind,module_name,imported_name,alias,relative_level,start_line,start_column,
                        resolved_path,resolved_symbol_id,resolution_status,confidence,diagnostic,ordinal)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (analysis.repository_id, commit_sha, analysis.analysis_version, item.import_id,
                     item.source_path, item.source_symbol_id, item.import_kind.value, item.module_name,
                     item.imported_name, item.alias, item.relative_level, item.start_line, item.start_column,
                     item.resolved_path, item.resolved_symbol_id, item.resolution_status.value,
                     item.confidence.value, item.diagnostic, item.ordinal),
                )
            for item in analysis.references:
                journal._connection.execute(
                    """INSERT INTO repository_symbol_references(
                        repository_id,commit_sha,analysis_version,reference_id,source_path,source_symbol_id,
                        target_path,target_symbol_id,reference_kind,expression,start_line,end_line,start_column,
                        end_column,resolution_status,confidence,diagnostic,ordinal)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (analysis.repository_id, commit_sha, analysis.analysis_version, item.reference_id,
                     item.source_path, item.source_symbol_id, item.target_path, item.target_symbol_id,
                     item.reference_kind.value, item.expression, item.start_line, item.end_line,
                     item.start_column, item.end_column, item.resolution_status.value,
                     item.confidence.value, item.diagnostic, item.ordinal),
                )
            for item in analysis.edges:
                journal._connection.execute(
                    """INSERT INTO repository_dependency_edges(
                        repository_id,commit_sha,analysis_version,edge_id,source_path,source_symbol_id,
                        target_path,target_symbol_id,edge_kind,resolution_status,confidence,
                        origin_reference_id,ordinal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (analysis.repository_id, commit_sha, analysis.analysis_version, item.edge_id,
                     item.source_path, item.source_symbol_id, item.target_path, item.target_symbol_id,
                     item.edge_kind.value, item.resolution_status.value, item.confidence.value,
                     item.origin_reference_id, item.ordinal),
                )
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="repository analysis write") from exc
    stored = get_repository_analysis(journal, analysis.repository_id, commit_sha,
                                     analysis.analysis_version, include_records=True)
    if stored is None:
        raise BridgeError("journal_corrupt", "Analysis disappeared after commit")
    return AnalysisPersistOutcome(stored, created=True, idempotent=False)


def _validate_counts(analysis: RepositoryAnalysis, *, error_code: str) -> None:
    if analysis.import_count != len(analysis.imports):
        raise BridgeError(error_code, "import_count does not match records")
    if analysis.reference_count != len(analysis.references):
        raise BridgeError(error_code, "reference_count does not match records")
    if analysis.dependency_edge_count != len(analysis.edges):
        raise BridgeError(error_code, "dependency_edge_count does not match records")
    resolved = sum(item.resolution_status is ResolutionStatus.RESOLVED for item in analysis.references)
    calls = sum(item.edge_kind is EdgeKind.CALL for item in analysis.edges)
    if analysis.resolved_reference_count != resolved:
        raise BridgeError(error_code, "resolved_reference_count does not match records")
    if analysis.call_edge_count != calls:
        raise BridgeError(error_code, "call_edge_count does not match records")
    if analysis.python_file_count < 0:
        raise BridgeError(error_code, "python_file_count must be non-negative")
    for expected, records, field in (
        (list(range(len(analysis.imports))), [item.ordinal for item in analysis.imports], "imports"),
        (list(range(len(analysis.references))), [item.ordinal for item in analysis.references], "references"),
        (list(range(len(analysis.edges))), [item.ordinal for item in analysis.edges], "edges"),
    ):
        if expected != records:
            raise BridgeError(error_code, f"{field} ordinals are not canonical")


def _same_analysis(left: RepositoryAnalysis, right: RepositoryAnalysis) -> bool:
    return (left.repository_id == right.repository_id and left.commit_sha == right.commit_sha
            and left.analysis_version == right.analysis_version
            and left.python_file_count == right.python_file_count
            and left.import_count == right.import_count
            and left.reference_count == right.reference_count
            and left.resolved_reference_count == right.resolved_reference_count
            and left.call_edge_count == right.call_edge_count
            and left.dependency_edge_count == right.dependency_edge_count
            and left.imports == right.imports and left.references == right.references
            and left.edges == right.edges)


def install_journal_code_relationship_api(journal_cls: Type[object]) -> None:
    setattr(journal_cls, "get_repository_analysis", get_repository_analysis)
    setattr(journal_cls, "list_repository_imports", list_repository_imports)
    setattr(journal_cls, "list_symbol_references", list_symbol_references)
    setattr(journal_cls, "list_dependency_edges", list_dependency_edges)
    setattr(journal_cls, "save_repository_analysis", save_repository_analysis)
