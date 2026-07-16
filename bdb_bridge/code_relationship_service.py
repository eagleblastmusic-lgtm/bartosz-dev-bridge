from __future__ import annotations

from collections import deque
from pathlib import PurePosixPath

from .code_relationship_analyzer import RepositoryRelationshipAnalyzer
from .code_relationship_models import (
    ANALYSIS_VERSION, MAX_GRAPH_DEPTH, MAX_GRAPH_NODES, MAX_QUERY_LENGTH, MAX_SEARCH_LIMIT,
    AnalysisImport, AnalysisPersistOutcome, DependencyEdge, EdgeKind, ReferenceKind,
    RepositoryAnalysis, ResolutionStatus, SearchResult, SymbolReference,
)
from .git_object_reader import GitObjectReader
from .protocol import BridgeError, validate_repo_relative_path
from .repository_index_models import RepositorySnapshot, RepositorySymbol


class RepositoryRelationshipService:
    def __init__(self, config, journal) -> None:
        self.config = config
        self.journal = journal
        self.reader = GitObjectReader(config.fixture_repo_path)

    def analyze(self, ref: str = "HEAD") -> AnalysisPersistOutcome:
        commit_sha = self.reader.resolve_commit(ref)
        snapshot = self._snapshot(commit_sha)
        analyzer = RepositoryRelationshipAnalyzer(
            repo_path=self.config.fixture_repo_path,
            repository_id=self.config.repository_id,
            now_fn=self.journal._now_fn,
        )
        return self.journal.save_repository_analysis(analyzer.build(snapshot))

    def analysis(self, ref: str = "HEAD") -> RepositoryAnalysis:
        return self._analysis(self.reader.resolve_commit(ref))

    def search(self, *, ref: str = "HEAD", query: str, kind: str = "all", limit: int = 50) -> tuple[SearchResult, ...]:
        query = _validate_query(query)
        limit = _bounded_int(limit, field="limit", lower=1, upper=MAX_SEARCH_LIMIT)
        if kind not in {"all", "file", "symbol"}:
            raise BridgeError("invalid_query", "kind must be all, file, or symbol")
        snapshot = self._snapshot(self.reader.resolve_commit(ref))
        needle, results = query.casefold(), []
        if kind in {"all", "file"}:
            for item in snapshot.files:
                match = _file_match(item.path, needle)
                if match:
                    rank, field = match
                    results.append(SearchResult("file", item.path, None, None, PurePosixPath(item.path).name,
                                                None, None, None, None, field, rank))
        if kind in {"all", "symbol"}:
            for file in snapshot.files:
                for symbol in file.symbols:
                    match = _symbol_match(file.path, symbol, needle)
                    if match:
                        rank, field = match
                        results.append(SearchResult("symbol", file.path, symbol.symbol_id, symbol.kind.value,
                                                    symbol.name, symbol.qualified_name, symbol.start_line,
                                                    symbol.signature, symbol.docstring_summary, field, rank))
        results.sort(key=lambda item: (item.rank, item.path, item.start_line or 0,
                                       item.qualified_name or item.name, item.symbol_id or ""))
        return tuple(results[:limit])

    def select_symbol(self, *, ref: str = "HEAD", symbol_id: str | None = None,
                      path: str | None = None, qualified_name: str | None = None) -> tuple[str, RepositorySymbol]:
        snapshot = self._snapshot(self.reader.resolve_commit(ref))
        if symbol_id:
            if path is not None or qualified_name is not None:
                raise BridgeError("invalid_payload", "Use symbol-id or path+qualified-name, not both")
            matches = [(file.path, symbol) for file in snapshot.files for symbol in file.symbols
                       if symbol.symbol_id == symbol_id]
        else:
            if path is None or qualified_name is None:
                raise BridgeError("invalid_payload", "Symbol selector requires symbol-id or path+qualified-name")
            path = validate_repo_relative_path(path)
            matches = [(file.path, symbol) for file in snapshot.files if file.path == path
                       for symbol in file.symbols if symbol.qualified_name == qualified_name]
        if not matches:
            raise BridgeError("symbol_not_found", "Requested symbol was not found")
        if len(matches) != 1:
            raise BridgeError("symbol_ambiguous", "Requested symbol selector is ambiguous")
        return matches[0]

    def references(self, *, ref: str = "HEAD", symbol_id: str | None = None,
                   path: str | None = None, qualified_name: str | None = None,
                   direction: str = "incoming", reference_kind: str = "all",
                   limit: int = 100) -> tuple[tuple[str, RepositorySymbol], tuple[SymbolReference, ...]]:
        selected = self.select_symbol(ref=ref, symbol_id=symbol_id, path=path, qualified_name=qualified_name)
        analysis = self._analysis(self.reader.resolve_commit(ref))
        if direction not in {"incoming", "outgoing"}:
            raise BridgeError("invalid_query", "direction must be incoming or outgoing")
        try:
            kind_enum = None if reference_kind == "all" else ReferenceKind(reference_kind)
        except ValueError as exc:
            raise BridgeError("invalid_query", "Unknown reference kind") from exc
        limit = _bounded_int(limit, field="limit", lower=1, upper=1000)
        symbol = selected[1]
        items = [item for item in analysis.references
                 if ((direction == "incoming" and item.target_symbol_id == symbol.symbol_id)
                     or (direction == "outgoing" and item.source_symbol_id == symbol.symbol_id))
                 and (kind_enum is None or item.reference_kind is kind_enum)]
        items.sort(key=lambda item: (item.source_path, item.start_line, item.start_column, item.reference_id))
        return selected, tuple(items[:limit])

    def callers(self, *, ref: str = "HEAD", symbol_id: str | None = None,
                path: str | None = None, qualified_name: str | None = None,
                limit: int = 100) -> tuple[tuple[str, RepositorySymbol], tuple[SymbolReference, ...]]:
        selected, references = self.references(ref=ref, symbol_id=symbol_id, path=path,
                                               qualified_name=qualified_name, direction="incoming",
                                               reference_kind=ReferenceKind.CALL.value, limit=limit)
        return selected, tuple(item for item in references
                               if item.resolution_status is ResolutionStatus.RESOLVED
                               and item.target_symbol_id == selected[1].symbol_id)

    def dependencies(self, *, ref: str = "HEAD", path: str, direction: str = "outgoing",
                     depth: int = 1, max_nodes: int = 200, edge_kind: str = "all") -> dict[str, object]:
        path = validate_repo_relative_path(path)
        if direction not in {"incoming", "outgoing"}:
            raise BridgeError("invalid_query", "direction must be incoming or outgoing")
        depth = _bounded_int(depth, field="depth", lower=1, upper=MAX_GRAPH_DEPTH)
        max_nodes = _bounded_int(max_nodes, field="max_nodes", lower=1, upper=MAX_GRAPH_NODES)
        try:
            kind_enum = None if edge_kind == "all" else EdgeKind(edge_kind)
        except ValueError as exc:
            raise BridgeError("invalid_query", "Unknown edge kind") from exc
        commit_sha = self.reader.resolve_commit(ref)
        snapshot = self._snapshot(commit_sha)
        if all(item.path != path for item in snapshot.files):
            raise BridgeError("repository_file_not_found", "Repository file was not found in snapshot")
        analysis = self._analysis(commit_sha)
        edges = tuple(item for item in analysis.edges if kind_enum is None or item.edge_kind is kind_enum)
        queue: deque[tuple[str, int]] = deque([(path, 0)])
        visited, selected_edges, truncated = {path: 0}, {}, False
        while queue:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            candidates = [item for item in edges
                          if (item.source_path == current if direction == "outgoing" else item.target_path == current)]
            candidates.sort(key=lambda item: (item.ordinal, item.edge_id))
            for item in candidates:
                neighbor = item.target_path if direction == "outgoing" else item.source_path
                if neighbor in visited:
                    selected_edges[item.edge_id] = item
                    continue
                if len(visited) >= max_nodes:
                    truncated = True
                    continue
                selected_edges[item.edge_id] = item
                visited[neighbor] = current_depth + 1
                queue.append((neighbor, current_depth + 1))
        selected = tuple(selected_edges.values())
        return {
            "commit_sha": commit_sha,
            "cycle": _has_directed_cycle(selected),
            "direction": direction,
            "edge_kind": edge_kind,
            "edges": [edge_dict(item) for item in sorted(selected, key=lambda item: (item.ordinal, item.edge_id))],
            "max_nodes": max_nodes,
            "nodes": [{"depth": node_depth, "path": node_path}
                      for node_path, node_depth in sorted(visited.items(), key=lambda pair: (pair[1], pair[0]))],
            "repository_id": self.config.repository_id,
            "root_path": path,
            "truncated": truncated,
        }

    def _snapshot(self, commit_sha: str) -> RepositorySnapshot:
        snapshot = self.journal.get_repository_snapshot(self.config.repository_id, commit_sha,
                                                        include_files=True, include_symbols=True)
        if snapshot is None:
            raise BridgeError("snapshot_not_found", "Repository snapshot not found; run `bdb bridge repo index` first")
        return snapshot

    def _analysis(self, commit_sha: str) -> RepositoryAnalysis:
        analysis = self.journal.get_repository_analysis(self.config.repository_id, commit_sha,
                                                        ANALYSIS_VERSION, include_records=True)
        if analysis is None:
            raise BridgeError("analysis_not_found", "Repository analysis not found; run `bdb bridge repo analyze` first")
        return analysis


def _has_directed_cycle(edges: tuple[DependencyEdge, ...]) -> bool:
    adjacency: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for edge in edges:
        adjacency.setdefault(edge.source_path, set()).add(edge.target_path)
        nodes.update((edge.source_path, edge.target_path))
    state: dict[str, int] = {}
    def visit(node: str) -> bool:
        marker = state.get(node, 0)
        if marker == 1: return True
        if marker == 2: return False
        state[node] = 1
        if any(visit(neighbor) for neighbor in sorted(adjacency.get(node, ()))): return True
        state[node] = 2
        return False
    return any(visit(node) for node in sorted(nodes) if state.get(node, 0) == 0)


def analysis_summary_dict(analysis: RepositoryAnalysis) -> dict[str, object]:
    return {"analysis_version": analysis.analysis_version, "analyzed_at": analysis.analyzed_at,
            "call_edge_count": analysis.call_edge_count, "commit_sha": analysis.commit_sha,
            "dependency_edge_count": analysis.dependency_edge_count, "import_count": analysis.import_count,
            "python_file_count": analysis.python_file_count, "reference_count": analysis.reference_count,
            "repository_id": analysis.repository_id, "resolved_reference_count": analysis.resolved_reference_count}


def import_dict(item: AnalysisImport) -> dict[str, object]:
    return {"alias": item.alias, "confidence": item.confidence.value, "diagnostic": item.diagnostic,
            "import_id": item.import_id, "import_kind": item.import_kind.value,
            "imported_name": item.imported_name, "module_name": item.module_name, "ordinal": item.ordinal,
            "relative_level": item.relative_level, "resolution_status": item.resolution_status.value,
            "resolved_path": item.resolved_path, "resolved_symbol_id": item.resolved_symbol_id,
            "source_path": item.source_path, "source_symbol_id": item.source_symbol_id,
            "start_column": item.start_column, "start_line": item.start_line}


def reference_dict(item: SymbolReference) -> dict[str, object]:
    return {"confidence": item.confidence.value, "diagnostic": item.diagnostic,
            "end_column": item.end_column, "end_line": item.end_line, "expression": item.expression,
            "ordinal": item.ordinal, "reference_id": item.reference_id,
            "reference_kind": item.reference_kind.value, "resolution_status": item.resolution_status.value,
            "source_path": item.source_path, "source_symbol_id": item.source_symbol_id,
            "start_column": item.start_column, "start_line": item.start_line,
            "target_path": item.target_path, "target_symbol_id": item.target_symbol_id}


def edge_dict(item: DependencyEdge) -> dict[str, object]:
    return {"confidence": item.confidence.value, "edge_id": item.edge_id,
            "edge_kind": item.edge_kind.value, "ordinal": item.ordinal,
            "origin_reference_id": item.origin_reference_id, "resolution_status": item.resolution_status.value,
            "source_path": item.source_path, "source_symbol_id": item.source_symbol_id,
            "target_path": item.target_path, "target_symbol_id": item.target_symbol_id}


def search_result_dict(item: SearchResult) -> dict[str, object]:
    return {"docstring_summary": item.docstring_summary, "match_field": item.match_field,
            "name": item.name, "path": item.path, "qualified_name": item.qualified_name,
            "rank": item.rank, "result_kind": item.result_kind, "signature": item.signature,
            "start_line": item.start_line, "symbol_id": item.symbol_id, "symbol_kind": item.symbol_kind}


def symbol_dict(path: str, symbol: RepositorySymbol) -> dict[str, object]:
    return {"kind": symbol.kind.value, "name": symbol.name, "path": path,
            "qualified_name": symbol.qualified_name, "start_line": symbol.start_line,
            "symbol_id": symbol.symbol_id}


def _validate_query(query: str) -> str:
    if not isinstance(query, str): raise BridgeError("invalid_query", "query must be text")
    query = query.strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise BridgeError("invalid_query", f"query length must be 1..{MAX_QUERY_LENGTH}")
    return query


def _bounded_int(value: int, *, field: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lower or value > upper:
        raise BridgeError("invalid_query", f"{field} must be in {lower}..{upper}")
    return value


def _file_match(path: str, needle: str) -> tuple[int, str] | None:
    value = path.casefold()
    if value == needle: return 3, "path"
    if value.startswith(needle): return 6, "path"
    if needle in value: return 9, "path"
    return None


def _symbol_match(path: str, symbol: RepositorySymbol, needle: str) -> tuple[int, str] | None:
    qualified, name, file_path = symbol.qualified_name.casefold(), symbol.name.casefold(), path.casefold()
    signature, docstring = (symbol.signature or "").casefold(), (symbol.docstring_summary or "").casefold()
    candidates: list[tuple[int, str]] = []
    if qualified == needle: candidates.append((1, "qualified_name"))
    if name == needle: candidates.append((2, "name"))
    if file_path == needle: candidates.append((3, "path"))
    if qualified.startswith(needle): candidates.append((4, "qualified_name"))
    if name.startswith(needle): candidates.append((5, "name"))
    if file_path.startswith(needle): candidates.append((6, "path"))
    if needle in qualified: candidates.append((7, "qualified_name"))
    if needle in name: candidates.append((8, "name"))
    if needle in file_path: candidates.append((9, "path"))
    if needle in signature: candidates.append((10, "signature"))
    if needle in docstring: candidates.append((10, "docstring_summary"))
    return min(candidates) if candidates else None
