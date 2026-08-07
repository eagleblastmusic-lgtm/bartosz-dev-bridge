from __future__ import annotations

import json
import re
import subprocess
import threading
from copy import deepcopy
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_object_reader import GitObjectReader, GitTreeEntry
from .protocol import BridgeError, path_matches, validate_repo_relative_path


SEARCH_TEXT_OPERATION = "search_text"
_MAX_QUERY_CHARS = 200
_MAX_RESULTS = 20
_MAX_PATH_PREFIXES = 8
_MAX_EXTENSIONS = 12
_DEFAULT_RESULTS = 12
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TRACKED_FILES = 10_000
_MAX_RESULT_BYTES = 3_000
_TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".graphql",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".liquid",
    ".md",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".svg",
    ".toml",
    ".sh",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_BASENAMES = {"package.json", "package-lock.json", "shopify.theme.toml"}
_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,12}$")


@dataclass(frozen=True)
class RepositorySnapshot:
    root: Path
    head: str
    entries: tuple[GitTreeEntry, ...]


_SNAPSHOT_CACHE: "OrderedDict[str, RepositorySnapshot]" = OrderedDict()
_SNAPSHOT_CACHE_LOCK = threading.RLock()
_SNAPSHOT_CACHE_LIMIT = 8
_SEARCH_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_SEARCH_CACHE_LOCK = threading.RLock()
_SEARCH_CACHE_LIMIT = 128


def repository_snapshot(config: Any) -> RepositorySnapshot:
    root = Path(config.fixture_repo_path).expanduser().resolve(strict=True)
    reader = GitObjectReader(root)
    head = reader.resolve_commit("HEAD")
    key = str(root)
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(key)
        if cached is not None and cached.head == head:
            _SNAPSHOT_CACHE.move_to_end(key)
            return cached
    reader.ensure_repository()
    snapshot = RepositorySnapshot(root=root, head=head, entries=reader.list_tree(head))
    with _SNAPSHOT_CACHE_LOCK:
        _SNAPSHOT_CACHE[key] = snapshot
        _SNAPSHOT_CACHE.move_to_end(key)
        while len(_SNAPSHOT_CACHE) > _SNAPSHOT_CACHE_LIMIT:
            _SNAPSHOT_CACHE.popitem(last=False)
    return snapshot


def _require_bool(payload: dict[str, Any], field: str, default: bool) -> bool:
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise BridgeError("invalid_payload", f"search_text payload.{field} must be boolean")
    return value


def _require_limit(payload: dict[str, Any]) -> int:
    value = payload.get("max_results", _DEFAULT_RESULTS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_RESULTS:
        raise BridgeError(
            "invalid_payload",
            f"search_text payload.max_results must be between 1 and {_MAX_RESULTS}",
        )
    return value


def _prefixes(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("path_prefixes", [])
    if (
        not isinstance(raw, list)
        or len(raw) > _MAX_PATH_PREFIXES
        or not all(isinstance(item, str) for item in raw)
    ):
        raise BridgeError(
            "invalid_payload",
            f"search_text payload.path_prefixes must be a list of at most {_MAX_PATH_PREFIXES} strings",
        )
    values: list[str] = []
    for item in raw:
        normalized = item.strip().replace("\\", "/").rstrip("/")
        if not normalized:
            raise BridgeError("invalid_payload", "search_text path prefixes must not be empty")
        values.append(validate_repo_relative_path(normalized))
    return tuple(values)


def _extensions(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("extensions", [])
    if (
        not isinstance(raw, list)
        or len(raw) > _MAX_EXTENSIONS
        or not all(isinstance(item, str) for item in raw)
    ):
        raise BridgeError(
            "invalid_payload",
            f"search_text payload.extensions must be a list of at most {_MAX_EXTENSIONS} strings",
        )
    values: list[str] = []
    for item in raw:
        normalized = item.lower()
        if _EXTENSION_RE.fullmatch(normalized) is None:
            raise BridgeError("invalid_payload", f"Unsafe search_text extension: {item}")
        values.append(normalized)
    return tuple(sorted(set(values)))


def _trim_line(value: str, limit: int = 240) -> str:
    collapsed = value.strip().replace("\t", " ")
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
    matches = result["matches"]
    while matches:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= _MAX_RESULT_BYTES:
            return result
        matches.pop()
        result["truncated"] = True
    return result


def _search_cache_key(
    config: Any,
    snapshot: RepositorySnapshot,
    *,
    query: str,
    case_sensitive: bool,
    max_results: int,
    prefixes: tuple[str, ...],
    extensions: tuple[str, ...],
) -> str:
    document = {
        "root": str(snapshot.root),
        "head": snapshot.head,
        "allowed_paths": list(config.allowed_paths),
        "query": query,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
        "prefixes": list(prefixes),
        "extensions": list(extensions),
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cached_search(key: str) -> dict[str, Any] | None:
    with _SEARCH_CACHE_LOCK:
        cached = _SEARCH_CACHE.get(key)
        if cached is None:
            return None
        _SEARCH_CACHE.move_to_end(key)
        result = deepcopy(cached)
    result["cache"] = {
        "schema": "bdb-repository-search-cache-v1",
        "status": "hit",
        "backend": "git_grep",
        "base_sha": result.get("base_sha"),
    }
    return result


def _store_search(key: str, result: dict[str, Any]) -> None:
    stored = deepcopy(result)
    stored.pop("cache", None)
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE[key] = stored
        _SEARCH_CACHE.move_to_end(key)
        while len(_SEARCH_CACHE) > _SEARCH_CACHE_LIMIT:
            _SEARCH_CACHE.popitem(last=False)


def search_repository(
    config: Any,
    payload: dict[str, Any],
    *,
    snapshot: RepositorySnapshot | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BridgeError("invalid_payload", "search_text payload must be an object")
    query = payload.get("query")
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > _MAX_QUERY_CHARS
        or "\x00" in query
        or "\r" in query
        or "\n" in query
    ):
        raise BridgeError(
            "invalid_payload",
            f"search_text payload.query must contain 1-{_MAX_QUERY_CHARS} characters on one line",
        )
    query = query.strip()
    case_sensitive = _require_bool(payload, "case_sensitive", False)
    max_results = _require_limit(payload)
    prefixes = _prefixes(payload)
    extensions = _extensions(payload)

    snapshot = snapshot or repository_snapshot(config)
    cache_key = _search_cache_key(
        config,
        snapshot,
        query=query,
        case_sensitive=case_sensitive,
        max_results=max_results,
        prefixes=prefixes,
        extensions=extensions,
    )
    cached = _cached_search(cache_key)
    if cached is not None:
        return cached
    root = snapshot.root
    head = snapshot.head
    entries = snapshot.entries
    if len(entries) > _MAX_TRACKED_FILES:
        entries = entries[:_MAX_TRACKED_FILES]
        tracked_truncated = True
    else:
        tracked_truncated = False

    needle = query if case_sensitive else query.casefold()
    matches: list[dict[str, Any]] = []
    total_matches = 0
    scanned_files = 0
    skipped_files = 0

    eligible: set[str] = set()
    for entry in entries:
        relative = entry.path
        try:
            normalized = validate_repo_relative_path(relative)
        except BridgeError:
            skipped_files += 1
            continue
        if not path_matches(normalized, config.allowed_paths):
            continue
        if prefixes and not any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in prefixes):
            continue
        suffix = Path(normalized).suffix.lower()
        if extensions:
            if suffix not in extensions:
                continue
        elif Path(normalized).name not in _TEXT_BASENAMES and suffix not in _TEXT_SUFFIXES:
            continue

        if entry.object_type != "blob" or entry.mode == "120000":
            skipped_files += 1
            continue
        if entry.size_bytes > _MAX_FILE_BYTES:
            skipped_files += 1
            continue
        scanned_files += 1
        eligible.add(normalized)

        path_haystack = normalized if case_sensitive else normalized.casefold()
        if needle in path_haystack:
            total_matches += 1
            if len(matches) < max_results:
                matches.append({"kind": "path", "path": normalized, "line": 0, "text": normalized})

    args = ["git", "-C", str(root), "grep", "-n", "-I", "-z", "-F"]
    if not case_sensitive:
        args.append("-i")
    args.extend(["--", query, head, "--"])
    args.extend(prefixes)
    try:
        completed = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            timeout=60.0,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BridgeError("invalid_config", "git executable is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("invalid_payload", "git grep timed out") from exc
    if completed.returncode not in {0, 1}:
        raise BridgeError("invalid_payload", "git grep failed")
    prefix = f"{head}:".encode("ascii")
    for raw_line in completed.stdout.splitlines():
        parts = raw_line.split(b"\0", 2)
        if len(parts) != 3 or not parts[0].startswith(prefix):
            skipped_files += 1
            continue
        try:
            normalized = validate_repo_relative_path(
                parts[0][len(prefix) :].decode("utf-8", errors="strict")
            )
            number = int(parts[1])
            line = parts[2].decode("utf-8", errors="strict")
        except (BridgeError, UnicodeError, ValueError):
            skipped_files += 1
            continue
        if normalized not in eligible:
            continue
        total_matches += 1
        if len(matches) < max_results:
            matches.append(
                {
                    "kind": "content",
                    "path": normalized,
                    "line": number,
                    "text": _trim_line(line),
                }
            )

    result = {
        "status": "success",
        "operation": SEARCH_TEXT_OPERATION,
        "query": query,
        "case_sensitive": case_sensitive,
        "matches": matches,
        "returned_matches": len(matches),
        "total_matches": total_matches,
        "truncated": tracked_truncated or total_matches > len(matches),
        "scanned_files": scanned_files,
        "skipped_files": skipped_files,
        "base_sha": head,
        "changed_files": [],
        "cache": {
            "schema": "bdb-repository-search-cache-v1",
            "status": "miss",
            "backend": "git_grep",
            "base_sha": head,
        },
    }
    bounded = _bounded_result(result)
    bounded["returned_matches"] = len(bounded["matches"])
    _store_search(cache_key, bounded)
    return bounded
