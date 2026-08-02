from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .git_object_reader import GitObjectReader
from .protocol import BridgeError, path_matches, validate_repo_relative_path
from .repository_search import RepositorySnapshot, repository_snapshot, search_repository
from .workspace_context import WorkspaceContextBuilder


INSPECT_BUNDLE_OPERATION = "inspect_bundle"
_MAX_SEARCHES = 8
_MAX_READS = 20
_MAX_READ_LINES = 1_000
_DEFAULT_READ_LINES = 400
_MAX_READ_BYTES = 64 * 1024
_MAX_TOTAL_CONTENT_BYTES = 512 * 1024
_MAX_TREE_PATHS = 3_000
_MAX_RESULT_BYTES = 850 * 1024
_COMPACT_MAX_READS = 10
_COMPACT_MAX_READ_BYTES = 3 * 1024
_COMPACT_MAX_TOTAL_CONTENT_BYTES = 12 * 1024
_COMPACT_MAX_TREE_PATHS = 80
_COMPACT_MAX_SYMBOLS = 20
_COMPACT_SEARCH_MATCHES = 3
_COMPACT_RESULT_BYTES = 20 * 1024
_READ_MERGE_GAP_LINES = 12


def _read_specs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("reads", [])
    if not isinstance(raw, list) or len(raw) > _MAX_READS:
        raise BridgeError(
            "invalid_payload", f"inspect_bundle reads must contain at most {_MAX_READS} items"
        )
    specs: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise BridgeError("invalid_payload", "inspect_bundle read items must be objects")
        path = item.get("path")
        if not isinstance(path, str):
            raise BridgeError("invalid_payload", "inspect_bundle read.path must be a string")
        start = item.get("start_line", 1)
        end = item.get("end_line", start + _DEFAULT_READ_LINES - 1 if type(start) is int else 0)
        if (
            type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
            or end - start + 1 > _MAX_READ_LINES
        ):
            raise BridgeError(
                "invalid_payload",
                f"inspect_bundle read ranges may contain at most {_MAX_READ_LINES} lines",
            )
        specs.append(
            {
                "path": validate_repo_relative_path(path),
                "start_line": start,
                "end_line": end,
                "source": "explicit",
            }
        )
    return specs


def _top_match_limit(payload: dict[str, Any]) -> int:
    value = payload.get("read_top_matches", 4)
    if value is True:
        return 4
    if value is False:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12:
        raise BridgeError(
            "invalid_payload", "inspect_bundle read_top_matches must be boolean or 0-12"
        )
    return value


def _densest_match_cluster(lines: list[int], *, max_span: int = 183) -> list[int]:
    ordered = sorted(lines)
    if not ordered:
        return [1]
    best_start = 0
    best_end = 0
    start = 0
    for end, line in enumerate(ordered):
        while line - ordered[start] > max_span:
            start += 1
        if end - start > best_end - best_start:
            best_start, best_end = start, end
    return ordered[best_start : best_end + 1]


def _preferred_match_lines(lines_by_search: dict[int, list[int]]) -> list[int]:
    """Prioritize the earliest (normally exact) search over later broad matches."""

    priorities = sorted(index for index, lines in lines_by_search.items() if lines)
    if not priorities:
        return [1]
    chosen = _densest_match_cluster(lines_by_search[priorities[0]])
    for search_index in priorities[1:]:
        candidate = chosen + lines_by_search[search_index]
        if max(candidate) - min(candidate) <= 183:
            chosen = candidate
    return sorted(chosen)


def _match_read_specs(searches: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    ordered_paths: list[str] = []
    lines_by_path: dict[str, dict[int, list[int]]] = {}
    match_lists = [search.get("matches", []) for search in searches]
    offset = 0
    while True:
        added = False
        for search_index, matches in enumerate(match_lists):
            if not isinstance(matches, list) or offset >= len(matches):
                continue
            match = matches[offset]
            path = match.get("path")
            if not isinstance(path, str):
                continue
            if path not in lines_by_path:
                if len(ordered_paths) >= limit:
                    continue
                ordered_paths.append(path)
                lines_by_path[path] = {}
            line = match.get("line")
            center = line if type(line) is int and line > 0 else 1
            lines_by_path[path].setdefault(search_index, []).append(center)
            added = True
        if all(not isinstance(items, list) or offset >= len(items) - 1 for items in match_lists):
            break
        if not added and not any(
            isinstance(items, list) and offset + 1 < len(items) for items in match_lists
        ):
            break
        offset += 1
    specs: list[dict[str, Any]] = []
    for path in ordered_paths:
        match_lines = _preferred_match_lines(lines_by_path[path])
        start = max(1, min(match_lines) - 8)
        end = max(match_lines) + 8
        specs.append(
            {
                "path": path,
                "start_line": start,
                "end_line": end,
                "source": "search_match",
            }
        )
    return specs


def _render_reads(
    config: Any,
    snapshot: RepositorySnapshot,
    specs: list[dict[str, Any]],
    *,
    max_reads: int = _MAX_READS,
    max_read_bytes: int = _MAX_READ_BYTES,
    max_total_content_bytes: int = _MAX_TOTAL_CONTENT_BYTES,
) -> tuple[list[dict[str, Any]], bool]:
    entries = {
        entry.path: entry
        for entry in snapshot.entries
        if entry.object_type == "blob"
        and entry.mode != "120000"
        and path_matches(entry.path, config.allowed_paths)
    }
    selected: list[tuple[dict[str, Any], Any]] = []
    selection_truncated = False
    for spec in specs:
        path = spec["path"]
        if not path_matches(path, config.allowed_paths):
            raise BridgeError("policy_denied", f"Path is not allowed by local policy: {path}")
        entry = entries.get(path)
        if entry is None:
            raise BridgeError("missing_file", f"Git snapshot does not contain a regular file: {path}")
        merge_indexes = [
            index
            for index, (existing, _existing_entry) in enumerate(selected)
            if existing["path"] == path
            and spec["start_line"] <= existing["end_line"] + _READ_MERGE_GAP_LINES
            and existing["start_line"] <= spec["end_line"] + _READ_MERGE_GAP_LINES
            and max(existing["end_line"], spec["end_line"])
            - min(existing["start_line"], spec["start_line"])
            + 1
            <= _MAX_READ_LINES
        ]
        if merge_indexes:
            first = merge_indexes[0]
            merged_specs = [selected[index][0] for index in merge_indexes] + [spec]
            sources: list[str] = []
            for merged in merged_specs:
                for source in merged["source"].split("+"):
                    if source not in sources:
                        sources.append(source)
            selected[first] = (
                {
                    **selected[first][0],
                    "start_line": min(item["start_line"] for item in merged_specs),
                    "end_line": max(item["end_line"] for item in merged_specs),
                    "source": "+".join(sources),
                },
                entry,
            )
            for index in reversed(merge_indexes[1:]):
                selected.pop(index)
            continue
        if len(selected) >= max_reads:
            selection_truncated = True
            continue
        selected.append((dict(spec), entry))

    reader = GitObjectReader(snapshot.root)
    object_shas = tuple(dict.fromkeys(entry.object_sha for _, entry in selected))
    blobs = reader.read_blobs(object_shas)
    rendered: list[dict[str, Any]] = []
    total_bytes = 0
    total_truncated = selection_truncated
    for spec, entry in selected:
        raw = blobs[entry.object_sha]
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            rendered.append({"path": entry.path, "error": "not_utf8", "source": spec["source"]})
            continue
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        start = spec["start_line"]
        if start > max(1, total_lines):
            rendered.append(
                {"path": entry.path, "error": "range_outside_file", "source": spec["source"]}
            )
            continue
        requested_end = spec["end_line"]
        end = min(requested_end, total_lines)
        content = "" if total_lines == 0 else "".join(lines[start - 1 : end])
        encoded = content.encode("utf-8")
        remaining = max(0, max_total_content_bytes - total_bytes)
        allowed = min(max_read_bytes, remaining)
        if allowed <= 0:
            total_truncated = True
            break
        content_truncated = len(encoded) > allowed
        if content_truncated:
            content = encoded[:allowed].decode("utf-8", errors="ignore")
            encoded = content.encode("utf-8")
            total_truncated = True
        returned_line_count = len(content.splitlines())
        returned_end = start + returned_line_count - 1 if returned_line_count else start - 1
        total_bytes += len(encoded)
        rendered.append(
            {
                "path": entry.path,
                "source": spec["source"],
                "start_line": start,
                "end_line": returned_end,
                "requested_end_line": requested_end,
                "total_lines": total_lines,
                "content": content,
                "content_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                "file_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "file_bytes": len(raw),
                "returned_bytes": len(encoded),
                "truncated": content_truncated,
                "range_complete": not content_truncated,
                "file_has_more": end < total_lines,
            }
        )
        if total_bytes >= max_total_content_bytes:
            total_truncated = True
            break
    return rendered, total_truncated


def _bound_result(result: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    while size() > _MAX_RESULT_BYTES and result["tree"]:
        result["tree"].pop()
        result["tree_truncated"] = True
    while size() > _MAX_RESULT_BYTES and result["reads"]:
        result["reads"].pop()
        result["reads_truncated"] = True
    if size() > _MAX_RESULT_BYTES:
        raise BridgeError("result_too_large", "inspect_bundle result exceeds its bounded limit")
    return result


def _focus_prefixes(payload: dict[str, Any], searches: list[dict[str, Any]]) -> tuple[str, ...]:
    values: list[str] = []
    for search in payload.get("searches", []):
        if not isinstance(search, dict):
            continue
        for prefix in search.get("path_prefixes", []):
            if isinstance(prefix, str) and prefix not in values:
                values.append(prefix.rstrip("/"))
    for read in payload.get("reads", []):
        if not isinstance(read, dict) or not isinstance(read.get("path"), str):
            continue
        path = read["path"]
        parent = path.rsplit("/", 1)[0] if "/" in path else path
        if parent not in values:
            values.append(parent)
    if values:
        return tuple(values)
    for search in searches:
        for match in search.get("matches", []):
            path = match.get("path")
            if not isinstance(path, str):
                continue
            parent = path.rsplit("/", 1)[0] if "/" in path else path
            if parent not in values:
                values.append(parent)
    return tuple(values[:8])


def _in_focus(path: str, prefixes: tuple[str, ...]) -> bool:
    return not prefixes or any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _compact_search(search: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": search.get("query"),
        "case_sensitive": search.get("case_sensitive"),
        "total_matches": search.get("total_matches"),
        "returned_matches": search.get("returned_matches"),
        "truncated": search.get("truncated"),
        "matches": list(search.get("matches", []))[:_COMPACT_SEARCH_MATCHES],
        "cache": search.get("cache"),
    }


def _parallel_searches(
    config: Any,
    searches: list[dict[str, Any]],
    snapshot: RepositorySnapshot,
) -> list[dict[str, Any]]:
    if len(searches) <= 1:
        return [search_repository(config, item, snapshot=snapshot) for item in searches]
    # All workers read the same immutable Git commit. Mutations remain serialized
    # by the Bridge queue; only independent git-grep reconnaissance is parallel.
    with ThreadPoolExecutor(max_workers=min(4, len(searches)), thread_name_prefix="bdb-inspect") as pool:
        return list(pool.map(lambda item: search_repository(config, item, snapshot=snapshot), searches))


def _compact_bound(result: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    while size() > _COMPACT_RESULT_BYTES:
        candidates = [
            read for read in result["reads"] if isinstance(read.get("content"), str) and read["content"]
        ]
        if candidates:
            largest = max(candidates, key=lambda read: len(read["content"].encode("utf-8")))
            raw = largest["content"].encode("utf-8")
            if len(raw) > 768:
                largest["content"] = raw[: max(768, len(raw) // 2)].decode("utf-8", errors="ignore")
                largest["returned_bytes"] = len(largest["content"].encode("utf-8"))
                largest["truncated"] = True
                result["reads_truncated"] = True
                continue
        removable = next(
            (search for search in reversed(result["searches"]) if search.get("matches")),
            None,
        )
        if removable is not None:
            removable["matches"].pop()
            removable["truncated"] = True
            continue
        if result["tree"]:
            result["tree"].pop()
            result["tree_truncated"] = True
            continue
        if result["context"]["symbols"]:
            result["context"]["symbols"].pop()
            result["context"]["symbols_truncated"] = True
            continue
        raise BridgeError("result_too_large", "Compact inspect_bundle result exceeds its limit")
    result["result_bytes"] = size()
    return result


def inspect_repository(config: Any, payload: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BridgeError("invalid_payload", "inspect_bundle payload must be an object")
    raw_searches = payload.get("searches", [])
    if not isinstance(raw_searches, list) or len(raw_searches) > _MAX_SEARCHES:
        raise BridgeError(
            "invalid_payload",
            f"inspect_bundle searches must contain at most {_MAX_SEARCHES} items",
        )
    if not all(isinstance(item, dict) for item in raw_searches):
        raise BridgeError("invalid_payload", "inspect_bundle search items must be objects")

    snapshot = repository_snapshot(config)
    searches = _parallel_searches(config, raw_searches, snapshot)
    specs = _read_specs(payload)
    specs.extend(_match_read_specs(searches, _top_match_limit(payload)))
    reads, reads_truncated = _render_reads(
        config,
        snapshot,
        specs,
        max_reads=_COMPACT_MAX_READS if compact else _MAX_READS,
        max_read_bytes=_COMPACT_MAX_READ_BYTES if compact else _MAX_READ_BYTES,
        max_total_content_bytes=(
            _COMPACT_MAX_TOTAL_CONTENT_BYTES if compact else _MAX_TOTAL_CONTENT_BYTES
        ),
    )
    context_builder = WorkspaceContextBuilder(config)
    context = context_builder.build_summary() if compact else context_builder.build()

    allowed_entries = [
        entry
        for entry in snapshot.entries
        if entry.object_type == "blob" and path_matches(entry.path, config.allowed_paths)
    ]
    focus_prefixes = _focus_prefixes(payload, searches)
    include_tree = payload.get("include_tree", not compact)
    include_symbols = payload.get("include_symbols", True)
    if not isinstance(include_tree, bool) or not isinstance(include_symbols, bool):
        raise BridgeError("invalid_payload", "inspect_bundle include_tree/include_symbols must be boolean")
    focused_entries = [entry for entry in allowed_entries if _in_focus(entry.path, focus_prefixes)]
    tree_limit = _COMPACT_MAX_TREE_PATHS if compact else _MAX_TREE_PATHS
    tree = [
        {"path": entry.path, "bytes": entry.size_bytes}
        for entry in focused_entries[:tree_limit]
    ] if include_tree else []
    if compact:
        focused_symbols: list[dict[str, Any]] = []
        for read in reads:
            path = read.get("path")
            content = read.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            focused_symbols.extend(
                context_builder.symbols_from_text(
                    path,
                    content,
                    _COMPACT_MAX_SYMBOLS - len(focused_symbols),
                    start_line=read.get("start_line", 1),
                )
            )
            if len(focused_symbols) >= _COMPACT_MAX_SYMBOLS:
                break
    else:
        focused_symbols = [
            symbol
            for symbol in context["symbols"]
            if isinstance(symbol, dict)
            and isinstance(symbol.get("path"), str)
            and _in_focus(symbol["path"], focus_prefixes)
        ]
    symbol_limit = _COMPACT_MAX_SYMBOLS if compact else len(focused_symbols)
    symbols = focused_symbols[:symbol_limit] if include_symbols else []
    rendered_searches = [_compact_search(search) for search in searches] if compact else searches
    result = {
        "status": "success",
        "operation": INSPECT_BUNDLE_OPERATION,
        "base_sha": snapshot.head,
        "changed_files": [],
        "context": {
            "source_clean": context["source_clean"],
            "controlled_clean": context["controlled_clean"],
            "source_changes": context["source_changes"],
            "source_changes_truncated": context["source_changes_truncated"],
            "source_changes_outside_scope": context["source_changes_outside_scope"],
            "symbols": symbols,
            "symbols_truncated": (
                context["symbols_truncated"] or len(focused_symbols) > len(symbols)
            ),
            "latest_promotion": context["latest_promotion"],
            "capabilities": context["capabilities"],
        },
        "tree": tree,
        "tree_truncated": include_tree and len(focused_entries) > len(tree),
        "tree_summary": {
            "allowed_files": len(allowed_entries),
            "focused_files": len(focused_entries),
            "focus_prefixes": list(focus_prefixes),
        },
        "searches": rendered_searches,
        "reads": reads,
        "reads_truncated": reads_truncated,
        "limits": {
            "searches": _MAX_SEARCHES,
            "reads": _COMPACT_MAX_READS if compact else _MAX_READS,
            "read_lines": _MAX_READ_LINES,
            "read_bytes": _COMPACT_MAX_READ_BYTES if compact else _MAX_READ_BYTES,
            "total_content_bytes": (
                _COMPACT_MAX_TOTAL_CONTENT_BYTES if compact else _MAX_TOTAL_CONTENT_BYTES
            ),
            "result_bytes": _COMPACT_RESULT_BYTES if compact else _MAX_RESULT_BYTES,
        },
        "response_profile": "compact" if compact else "full",
        "performance": {
            "parallel_searches": len(raw_searches) > 1,
            "search_workers": min(4, len(raw_searches)),
            "search_cache_hits": sum(
                1 for item in searches if item.get("cache", {}).get("status") == "hit"
            ),
            "index_backend": "git_grep",
        },
    }
    return _compact_bound(result) if compact else _bound_result(result)
