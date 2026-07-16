from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .code_relationship_models import ANALYSIS_VERSION, DependencyEdge, RepositoryAnalysis, SearchResult
from .code_relationship_service import RepositoryRelationshipService
from .context_pack_models import (
    CONTEXT_PACK_VERSION,
    DEFAULT_GATE_MAX_FILES,
    DEFAULT_GATE_MAX_RELATIONSHIPS,
    DEFAULT_GATE_MAX_SYMBOLS,
    DEFAULT_GATE_SAMPLE_BYTES,
    DEFAULT_GATE_SAMPLE_FILES,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_DEPTH,
    MAX_CONTEXT_FILES,
    MAX_CONTEXT_SOURCE_FILE_BYTES,
    MAX_EXCERPT_LINES,
    MIN_CONTEXT_BYTES,
    ContextDirection,
    ContextExcerpt,
    ContextFile,
    ContextPack,
    GateCheck,
    LargeRepositoryGateResult,
)
from .git_object_reader import GitObjectReader
from .protocol import BridgeError, validate_repo_relative_path
from .repository_index_models import FileKind, RepositoryFile, RepositorySnapshot, RepositorySymbol


@dataclass
class _Candidate:
    priority: int
    reasons: set[str] = field(default_factory=set)
    hints: list[tuple[int, int, str, int]] = field(default_factory=list)

    def add(self, priority: int, reason: str, hint: tuple[int, int, str, int] | None = None) -> None:
        self.priority = min(self.priority, priority)
        self.reasons.add(reason)
        if hint is not None:
            self.hints.append(hint)


class ContextPackService:
    def __init__(self, config, journal) -> None:
        self.config = config
        self.journal = journal
        self.reader = GitObjectReader(config.fixture_repo_path)
        self.relationships = RepositoryRelationshipService(config, journal)

    def build(
        self,
        *,
        ref: str = "HEAD",
        query: str | None = None,
        symbol_id: str | None = None,
        path: str | None = None,
        qualified_name: str | None = None,
        direction: str = "both",
        depth: int = 2,
        max_files: int = 20,
        max_bytes: int = 64 * 1024,
        max_excerpt_lines: int = 80,
    ) -> ContextPack:
        direction_enum = _direction(direction)
        depth = _bounded_int(depth, field="depth", lower=0, upper=MAX_CONTEXT_DEPTH)
        max_files = _bounded_int(max_files, field="max_files", lower=1, upper=MAX_CONTEXT_FILES)
        max_bytes = _bounded_int(max_bytes, field="max_bytes", lower=MIN_CONTEXT_BYTES, upper=MAX_CONTEXT_BYTES)
        max_excerpt_lines = _bounded_int(
            max_excerpt_lines, field="max_excerpt_lines", lower=1, upper=MAX_EXCERPT_LINES
        )
        commit_sha = self.reader.resolve_commit(ref)
        snapshot = self._snapshot(commit_sha)
        analysis = self._analysis(commit_sha)
        files = {item.path: item for item in snapshot.files}
        symbols = {
            symbol.symbol_id: (file.path, symbol)
            for file in snapshot.files
            for symbol in file.symbols
        }
        candidates: dict[str, _Candidate] = {}
        seed_kind, seed_value, seed_path, seed_symbol, search_results = self._seed(
            ref=ref,
            snapshot=snapshot,
            query=query,
            symbol_id=symbol_id,
            path=path,
            qualified_name=qualified_name,
        )
        self._candidate(candidates, seed_path, 0, f"seed:{seed_kind}")
        if seed_symbol is not None:
            candidates[seed_path].hints.append(
                (seed_symbol.start_line, seed_symbol.end_line, "seed-symbol", 0)
            )
        for item in search_results:
            if item.path not in files:
                continue
            priority = 10 + item.rank
            hint = None
            if item.symbol_id and item.symbol_id in symbols:
                _, symbol = symbols[item.symbol_id]
                hint = (symbol.start_line, symbol.end_line, f"search:{item.match_field}", priority)
            self._candidate(candidates, item.path, priority, f"search:{item.match_field}", hint)

        if seed_symbol is not None:
            self._symbol_neighbors(
                analysis, seed_symbol.symbol_id, candidates, symbols, direction_enum
            )
        self._dependency_neighbors(
            analysis, seed_path, candidates, symbols, direction_enum, depth
        )

        selected: list[ContextFile] = []
        source_bytes = 0
        truncated = False
        ordered = sorted(candidates.items(), key=lambda pair: (pair[1].priority, pair[0]))
        for candidate_path, candidate in ordered:
            if len(selected) >= max_files:
                truncated = True
                break
            record = files.get(candidate_path)
            if record is None:
                continue
            context_file, used_bytes, file_truncated = self._materialize_file(
                record,
                candidate,
                max_excerpt_lines=max_excerpt_lines,
                remaining_bytes=max_bytes - source_bytes,
            )
            if context_file is None:
                truncated = True
                continue
            selected.append(context_file)
            source_bytes += used_bytes
            truncated = truncated or file_truncated
            if source_bytes >= max_bytes:
                if len(selected) < len(ordered):
                    truncated = True
                break

        if not selected:
            raise BridgeError("context_budget_too_small", "No context file fits the requested source budget")
        without_hash = {
            "candidate_count": len(candidates),
            "commit_sha": commit_sha,
            "depth": depth,
            "direction": direction_enum.value,
            "excerpt_count": sum(len(item.excerpts) for item in selected),
            "files": [_context_file_dict(item) for item in selected],
            "max_bytes": max_bytes,
            "max_excerpt_lines": max_excerpt_lines,
            "max_files": max_files,
            "pack_version": CONTEXT_PACK_VERSION,
            "repository_id": self.config.repository_id,
            "seed_kind": seed_kind,
            "seed_value": seed_value,
            "selected_file_count": len(selected),
            "source_bytes": source_bytes,
            "truncated": truncated,
        }
        pack_sha256 = hashlib.sha256(_canonical_bytes(without_hash)).hexdigest()
        return ContextPack(
            pack_version=CONTEXT_PACK_VERSION,
            repository_id=self.config.repository_id,
            commit_sha=commit_sha,
            seed_kind=seed_kind,
            seed_value=seed_value,
            direction=direction_enum,
            depth=depth,
            max_files=max_files,
            max_bytes=max_bytes,
            max_excerpt_lines=max_excerpt_lines,
            candidate_count=len(candidates),
            selected_file_count=len(selected),
            excerpt_count=sum(len(item.excerpts) for item in selected),
            source_bytes=source_bytes,
            truncated=truncated,
            files=tuple(selected),
            pack_sha256=pack_sha256,
        )

    def gate(
        self,
        *,
        ref: str = "HEAD",
        max_files: int = DEFAULT_GATE_MAX_FILES,
        max_symbols: int = DEFAULT_GATE_MAX_SYMBOLS,
        max_relationships: int = DEFAULT_GATE_MAX_RELATIONSHIPS,
        sample_max_files: int = DEFAULT_GATE_SAMPLE_FILES,
        sample_max_bytes: int = DEFAULT_GATE_SAMPLE_BYTES,
    ) -> LargeRepositoryGateResult:
        max_files = _bounded_int(max_files, field="max_files", lower=1, upper=1_000_000)
        max_symbols = _bounded_int(max_symbols, field="max_symbols", lower=1, upper=10_000_000)
        max_relationships = _bounded_int(
            max_relationships, field="max_relationships", lower=1, upper=20_000_000
        )
        sample_max_files = _bounded_int(
            sample_max_files, field="sample_max_files", lower=1, upper=MAX_CONTEXT_FILES
        )
        sample_max_bytes = _bounded_int(
            sample_max_bytes,
            field="sample_max_bytes",
            lower=MIN_CONTEXT_BYTES,
            upper=MAX_CONTEXT_BYTES,
        )
        commit_sha = self.reader.resolve_commit(ref)
        snapshot = self._snapshot(commit_sha)
        analysis = self._analysis(commit_sha)
        symbol_count = sum(len(file.symbols) for file in snapshot.files)
        relationship_count = len(analysis.imports) + len(analysis.references) + len(analysis.edges)
        checks = [
            GateCheck(
                "snapshot_identity",
                snapshot.repository_id == self.config.repository_id and snapshot.commit_sha == commit_sha,
                f"repository={snapshot.repository_id} commit={snapshot.commit_sha}",
            ),
            GateCheck(
                "analysis_identity",
                analysis.repository_id == self.config.repository_id
                and analysis.commit_sha == commit_sha
                and analysis.analysis_version == ANALYSIS_VERSION,
                f"repository={analysis.repository_id} commit={analysis.commit_sha} version={analysis.analysis_version}",
            ),
            GateCheck(
                "snapshot_file_count",
                snapshot.file_count == len(snapshot.files),
                f"declared={snapshot.file_count} actual={len(snapshot.files)}",
            ),
            GateCheck(
                "snapshot_symbol_count",
                snapshot.symbol_count == symbol_count,
                f"declared={snapshot.symbol_count} actual={symbol_count}",
            ),
            GateCheck(
                "analysis_import_count",
                analysis.import_count == len(analysis.imports),
                f"declared={analysis.import_count} actual={len(analysis.imports)}",
            ),
            GateCheck(
                "analysis_reference_count",
                analysis.reference_count == len(analysis.references),
                f"declared={analysis.reference_count} actual={len(analysis.references)}",
            ),
            GateCheck(
                "analysis_edge_count",
                analysis.dependency_edge_count == len(analysis.edges),
                f"declared={analysis.dependency_edge_count} actual={len(analysis.edges)}",
            ),
            GateCheck("file_limit", snapshot.file_count <= max_files, f"{snapshot.file_count}<={max_files}"),
            GateCheck("symbol_limit", symbol_count <= max_symbols, f"{symbol_count}<={max_symbols}"),
            GateCheck(
                "relationship_limit",
                relationship_count <= max_relationships,
                f"{relationship_count}<={max_relationships}",
            ),
        ]
        sample_pack = self._sample_pack(
            ref=ref,
            snapshot=snapshot,
            max_files=sample_max_files,
            max_bytes=sample_max_bytes,
        )
        if sample_pack is None:
            checks.append(GateCheck("sample_context_pack", True, "empty repository; no sample seed"))
            sample_hash = None
            sample_selected = 0
            sample_source_bytes = 0
        else:
            expected_hash = hashlib.sha256(
                _canonical_bytes(_context_pack_dict(sample_pack, include_hash=False))
            ).hexdigest()
            checks.extend(
                (
                    GateCheck(
                        "sample_context_budget",
                        sample_pack.selected_file_count <= sample_max_files
                        and sample_pack.source_bytes <= sample_max_bytes,
                        f"files={sample_pack.selected_file_count}/{sample_max_files} "
                        f"bytes={sample_pack.source_bytes}/{sample_max_bytes}",
                    ),
                    GateCheck(
                        "sample_context_hash",
                        sample_pack.pack_sha256 == expected_hash,
                        sample_pack.pack_sha256,
                    ),
                )
            )
            sample_hash = sample_pack.pack_sha256
            sample_selected = sample_pack.selected_file_count
            sample_source_bytes = sample_pack.source_bytes
        metrics: dict[str, int | float | str | bool] = {
            "analysis_version": analysis.analysis_version,
            "binary_file_count": snapshot.binary_file_count,
            "file_count": snapshot.file_count,
            "python_file_count": snapshot.python_file_count,
            "relationship_count": relationship_count,
            "resolved_reference_ratio": (
                analysis.resolved_reference_count / analysis.reference_count
                if analysis.reference_count
                else 1.0
            ),
            "sample_selected_files": sample_selected,
            "sample_source_bytes": sample_source_bytes,
            "symbol_count": symbol_count,
            "text_file_count": snapshot.text_file_count,
        }
        return LargeRepositoryGateResult(
            gate_version=CONTEXT_PACK_VERSION,
            repository_id=self.config.repository_id,
            commit_sha=commit_sha,
            passed=all(item.passed for item in checks),
            metrics=metrics,
            checks=tuple(checks),
            sample_pack_sha256=sample_hash,
        )

    def _sample_pack(
        self,
        *,
        ref: str,
        snapshot: RepositorySnapshot,
        max_files: int,
        max_bytes: int,
    ) -> ContextPack | None:
        symbols = [(file.path, symbol) for file in snapshot.files for symbol in file.symbols]
        symbols.sort(key=lambda item: (item[0], item[1].ordinal, item[1].symbol_id))
        if symbols:
            path, symbol = symbols[0]
            return self.build(
                ref=ref,
                path=path,
                qualified_name=symbol.qualified_name,
                depth=2,
                max_files=max_files,
                max_bytes=max_bytes,
                max_excerpt_lines=80,
            )
        text_files = sorted(
            file.path
            for file in snapshot.files
            if file.file_kind is FileKind.REGULAR and file.is_text
        )
        if not text_files:
            return None
        return self.build(
            ref=ref,
            path=text_files[0],
            depth=1,
            max_files=max_files,
            max_bytes=max_bytes,
            max_excerpt_lines=80,
        )

    def _seed(
        self,
        *,
        ref: str,
        snapshot: RepositorySnapshot,
        query: str | None,
        symbol_id: str | None,
        path: str | None,
        qualified_name: str | None,
    ) -> tuple[str, str, str, RepositorySymbol | None, tuple[SearchResult, ...]]:
        modes = int(query is not None) + int(symbol_id is not None) + int(path is not None)
        if modes != 1:
            raise BridgeError(
                "invalid_payload",
                "Context seed requires exactly one of query, symbol-id, or path",
            )
        if query is not None:
            query = query.strip()
            results = self.relationships.search(ref=ref, query=query, kind="all", limit=100)
            if not results:
                raise BridgeError("context_seed_not_found", "Context query did not match a file or symbol")
            first = results[0]
            symbol = self._symbol_by_id(snapshot, first.symbol_id) if first.symbol_id else None
            return "query", query, first.path, symbol, results
        if symbol_id is not None:
            if qualified_name is not None:
                raise BridgeError("invalid_payload", "qualified-name requires a path selector")
            selected_path, symbol = self.relationships.select_symbol(ref=ref, symbol_id=symbol_id)
            return "symbol", symbol.symbol_id, selected_path, symbol, ()
        assert path is not None
        path = validate_repo_relative_path(path)
        if qualified_name is not None:
            selected_path, symbol = self.relationships.select_symbol(
                ref=ref, path=path, qualified_name=qualified_name
            )
            return "symbol", f"{selected_path}:{symbol.qualified_name}", selected_path, symbol, ()
        if all(item.path != path for item in snapshot.files):
            raise BridgeError("repository_file_not_found", "Repository file was not found in snapshot")
        return "file", path, path, None, ()

    def _symbol_by_id(self, snapshot: RepositorySnapshot, symbol_id: str) -> RepositorySymbol | None:
        matches = [
            symbol
            for file in snapshot.files
            for symbol in file.symbols
            if symbol.symbol_id == symbol_id
        ]
        return matches[0] if len(matches) == 1 else None

    def _symbol_neighbors(
        self,
        analysis: RepositoryAnalysis,
        symbol_id: str,
        candidates: dict[str, _Candidate],
        symbols: dict[str, tuple[str, RepositorySymbol]],
        direction: ContextDirection,
    ) -> None:
        for reference in sorted(analysis.references, key=lambda item: (item.ordinal, item.reference_id)):
            if direction in {ContextDirection.INCOMING, ContextDirection.BOTH} and (
                reference.target_symbol_id == symbol_id
            ):
                self._candidate(
                    candidates,
                    reference.source_path,
                    20,
                    f"incoming:{reference.reference_kind.value}",
                    (
                        reference.start_line,
                        reference.end_line,
                        f"incoming:{reference.reference_kind.value}",
                        20,
                    ),
                )
            if direction in {ContextDirection.OUTGOING, ContextDirection.BOTH} and (
                reference.source_symbol_id == symbol_id
            ):
                if reference.target_path is None:
                    continue
                hint = None
                if reference.target_symbol_id and reference.target_symbol_id in symbols:
                    _, target = symbols[reference.target_symbol_id]
                    hint = (
                        target.start_line,
                        target.end_line,
                        f"outgoing:{reference.reference_kind.value}",
                        20,
                    )
                self._candidate(
                    candidates,
                    reference.target_path,
                    20,
                    f"outgoing:{reference.reference_kind.value}",
                    hint,
                )

    def _dependency_neighbors(
        self,
        analysis: RepositoryAnalysis,
        seed_path: str,
        candidates: dict[str, _Candidate],
        symbols: dict[str, tuple[str, RepositorySymbol]],
        direction: ContextDirection,
        depth: int,
    ) -> None:
        if depth <= 0:
            return
        directions = (
            ("outgoing",)
            if direction is ContextDirection.OUTGOING
            else ("incoming",)
            if direction is ContextDirection.INCOMING
            else ("outgoing", "incoming")
        )
        ordered_edges = sorted(analysis.edges, key=lambda item: (item.ordinal, item.edge_id))
        outgoing: dict[str, list[DependencyEdge]] = {}
        incoming: dict[str, list[DependencyEdge]] = {}
        for edge in ordered_edges:
            outgoing.setdefault(edge.source_path, []).append(edge)
            incoming.setdefault(edge.target_path, []).append(edge)
        for relation_direction in directions:
            adjacency = outgoing if relation_direction == "outgoing" else incoming
            queue: deque[tuple[str, int]] = deque(((seed_path, 0),))
            visited = {seed_path}
            while queue:
                current, level = queue.popleft()
                if level >= depth:
                    continue
                for edge in adjacency.get(current, ()):
                    neighbor = edge.target_path if relation_direction == "outgoing" else edge.source_path
                    priority = 30 + (level * 10)
                    hint = _edge_symbol_hint(edge, symbols, relation_direction, priority)
                    self._candidate(
                        candidates,
                        neighbor,
                        priority,
                        f"dependency:{relation_direction}:{edge.edge_kind.value}",
                        hint,
                    )
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append((neighbor, level + 1))

    def _materialize_file(
        self,
        record: RepositoryFile,
        candidate: _Candidate,
        *,
        max_excerpt_lines: int,
        remaining_bytes: int,
    ) -> tuple[ContextFile | None, int, bool]:
        reason = ",".join(sorted(candidate.reasons))
        if _is_sensitive_path(record.path):
            return ContextFile(record.path, record.language, record.content_sha256, record.size_bytes, reason, candidate.priority, "sensitive_path", ()), 0, False
        if record.file_kind is not FileKind.REGULAR:
            return ContextFile(record.path, record.language, record.content_sha256, record.size_bytes, reason, candidate.priority, "metadata_only", ()), 0, False
        if not record.is_text:
            return ContextFile(record.path, record.language, record.content_sha256, record.size_bytes, reason, candidate.priority, "binary", ()), 0, False
        if record.size_bytes > MAX_CONTEXT_SOURCE_FILE_BYTES:
            return ContextFile(record.path, record.language, record.content_sha256, record.size_bytes, reason, candidate.priority, "source_file_too_large", ()), 0, False
        data = self.reader.read_blob(record.object_sha)
        if hashlib.sha256(data).hexdigest() != record.content_sha256:
            raise BridgeError("context_source_conflict", "Indexed source checksum does not match Git blob")
        try:
            source = data.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise BridgeError("context_source_conflict", "Indexed text blob is not valid UTF-8") from exc
        lines = source.splitlines()
        hints = list(candidate.hints)
        if not hints:
            if record.symbols:
                for symbol in sorted(record.symbols, key=lambda item: (item.ordinal, item.symbol_id))[:3]:
                    hints.append((symbol.start_line, symbol.end_line, "file-outline", 100 + symbol.ordinal))
            else:
                hints.append((1, min(len(lines), max_excerpt_lines), "file-head", 100))
        excerpts: list[ContextExcerpt] = []
        used = 0
        file_truncated = False
        for start, end, hint_reason, _priority in _normalize_hints(hints, line_count=len(lines), max_lines=max_excerpt_lines):
            text = _line_numbered(lines, start, end)
            encoded = len(text.encode("utf-8"))
            if encoded > remaining_bytes - used:
                file_truncated = True
                continue
            excerpts.append(ContextExcerpt(start, end, hint_reason, text))
            used += encoded
        if not excerpts and remaining_bytes > 0 and lines:
            start, end = 1, min(len(lines), max_excerpt_lines)
            text = _line_numbered(lines, start, end)
            encoded = len(text.encode("utf-8"))
            if encoded <= remaining_bytes:
                excerpts.append(ContextExcerpt(start, end, "file-head", text))
                used += encoded
            else:
                return None, 0, True
        return ContextFile(record.path, record.language, record.content_sha256, record.size_bytes, reason, candidate.priority, None, tuple(excerpts)), used, file_truncated

    def _candidate(
        self,
        candidates: dict[str, _Candidate],
        path: str,
        priority: int,
        reason: str,
        hint: tuple[int, int, str, int] | None = None,
    ) -> None:
        candidate = candidates.setdefault(path, _Candidate(priority))
        candidate.add(priority, reason, hint)

    def _snapshot(self, commit_sha: str) -> RepositorySnapshot:
        snapshot = self.journal.get_repository_snapshot(
            self.config.repository_id, commit_sha, include_files=True, include_symbols=True
        )
        if snapshot is None:
            raise BridgeError("snapshot_not_found", "Repository snapshot not found; run `bdb bridge repo index` first")
        return snapshot

    def _analysis(self, commit_sha: str) -> RepositoryAnalysis:
        analysis = self.journal.get_repository_analysis(
            self.config.repository_id, commit_sha, ANALYSIS_VERSION, include_records=True
        )
        if analysis is None:
            raise BridgeError("analysis_not_found", "Repository analysis not found; run `bdb bridge repo analyze` first")
        return analysis


def _is_sensitive_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in {"id_rsa", "id_ed25519", "credentials.json", "service-account.json"}:
        return True
    return pure.suffix.casefold() in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}


def _direction(value: str) -> ContextDirection:
    try:
        return ContextDirection(value)
    except ValueError as exc:
        raise BridgeError("invalid_query", "direction must be incoming, outgoing, or both") from exc


def _bounded_int(value: int, *, field: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lower or value > upper:
        raise BridgeError("invalid_query", f"{field} must be in {lower}..{upper}")
    return value


def _edge_symbol_hint(
    edge: DependencyEdge,
    symbols: dict[str, tuple[str, RepositorySymbol]],
    direction: str,
    priority: int,
) -> tuple[int, int, str, int] | None:
    symbol_id = edge.target_symbol_id if direction == "outgoing" else edge.source_symbol_id
    if symbol_id is None or symbol_id not in symbols:
        return None
    _, symbol = symbols[symbol_id]
    return symbol.start_line, symbol.end_line, f"dependency:{direction}:{edge.edge_kind.value}", priority


def _normalize_hints(
    hints: list[tuple[int, int, str, int]], *, line_count: int, max_lines: int
) -> tuple[tuple[int, int, str, int], ...]:
    if line_count <= 0:
        return ()
    ranges: list[tuple[int, int, set[str], int]] = []
    seen = set()
    for start, end, reason, priority in hints:
        start = max(1, min(int(start), line_count))
        end = max(start, min(int(end), line_count, start + max_lines - 1))
        key = (start, end, reason, priority)
        if key in seen:
            continue
        seen.add(key)
        ranges.append((start, end, {reason}, priority))
    ranges.sort(key=lambda item: (item[0], item[1], item[3], sorted(item[2])))
    merged: list[tuple[int, int, set[str], int]] = []
    for start, end, reasons, priority in ranges:
        if merged and start <= merged[-1][1] + 1:
            previous_start, previous_end, previous_reasons, previous_priority = merged[-1]
            merged[-1] = (
                previous_start,
                max(previous_end, end),
                previous_reasons | reasons,
                min(previous_priority, priority),
            )
        else:
            merged.append((start, end, reasons, priority))
    result = [
        (start, end, ",".join(sorted(reasons)), priority)
        for start, end, reasons, priority in merged
    ]
    result.sort(key=lambda item: (item[3], item[0], item[1], item[2]))
    return tuple(result)


def _line_numbered(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _context_file_dict(item: ContextFile) -> dict[str, object]:
    return {
        "content_sha256": item.content_sha256,
        "excerpts": [
            {"end_line": excerpt.end_line, "reason": excerpt.reason, "start_line": excerpt.start_line, "text": excerpt.text}
            for excerpt in item.excerpts
        ],
        "language": item.language,
        "omitted_reason": item.omitted_reason,
        "path": item.path,
        "priority": item.priority,
        "selection_reason": item.selection_reason,
        "size_bytes": item.size_bytes,
    }


def _context_pack_dict(pack: ContextPack, *, include_hash: bool = True) -> dict[str, object]:
    payload = {
        "candidate_count": pack.candidate_count,
        "commit_sha": pack.commit_sha,
        "depth": pack.depth,
        "direction": pack.direction.value,
        "excerpt_count": pack.excerpt_count,
        "files": [_context_file_dict(item) for item in pack.files],
        "max_bytes": pack.max_bytes,
        "max_excerpt_lines": pack.max_excerpt_lines,
        "max_files": pack.max_files,
        "pack_version": pack.pack_version,
        "repository_id": pack.repository_id,
        "seed_kind": pack.seed_kind,
        "seed_value": pack.seed_value,
        "selected_file_count": pack.selected_file_count,
        "source_bytes": pack.source_bytes,
        "truncated": pack.truncated,
    }
    if include_hash:
        payload["pack_sha256"] = pack.pack_sha256
    return payload


def context_pack_dict(pack: ContextPack) -> dict[str, object]:
    return _context_pack_dict(pack)


def gate_result_dict(result: LargeRepositoryGateResult) -> dict[str, object]:
    return {
        "checks": [{"detail": item.detail, "name": item.name, "passed": item.passed} for item in result.checks],
        "commit_sha": result.commit_sha,
        "gate_version": result.gate_version,
        "metrics": result.metrics,
        "passed": result.passed,
        "repository_id": result.repository_id,
        "sample_pack_sha256": result.sample_pack_sha256,
    }


def render_context_markdown(pack: ContextPack) -> str:
    lines = [
        "# Repository context pack",
        "",
        f"- repository: `{pack.repository_id}`",
        f"- commit: `{pack.commit_sha}`",
        f"- seed: `{pack.seed_kind}:{pack.seed_value}`",
        f"- direction/depth: `{pack.direction.value}/{pack.depth}`",
        f"- pack SHA-256: `{pack.pack_sha256}`",
        f"- files/excerpts/source bytes: `{pack.selected_file_count}/{pack.excerpt_count}/{pack.source_bytes}`",
        f"- truncated: `{str(pack.truncated).lower()}`",
    ]
    for item in pack.files:
        lines.extend(("", f"## `{item.path}`", "", f"Reason: `{item.selection_reason}`; language: `{item.language}`; content SHA-256: `{item.content_sha256}`."))
        if item.omitted_reason:
            lines.extend(("", f"Source omitted: `{item.omitted_reason}`."))
            continue
        for excerpt in item.excerpts:
            lines.extend(("", f"### Lines {excerpt.start_line}–{excerpt.end_line} — {excerpt.reason}", "", *[f"    {line}" for line in excerpt.text.splitlines()]))
    return "\n".join(lines) + "\n"
