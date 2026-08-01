from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .protocol import BridgeError, path_matches, validate_repo_relative_path
from .workspace_manager import Git


SEARCH_TEXT_OPERATION = "search_text"
_MAX_QUERY_CHARS = 200
_MAX_RESULTS = 20
_DEFAULT_RESULTS = 12
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TRACKED_FILES = 10_000
_MAX_RESULT_BYTES = 3_000
_TEXT_SUFFIXES = {
    ".css",
    ".graphql",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".liquid",
    ".md",
    ".mjs",
    ".scss",
    ".svg",
    ".toml",
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
    if not isinstance(raw, list) or len(raw) > 8 or not all(isinstance(item, str) for item in raw):
        raise BridgeError("invalid_payload", "search_text payload.path_prefixes must be a list of at most 8 strings")
    values: list[str] = []
    for item in raw:
        normalized = item.strip().replace("\\", "/").rstrip("/")
        if not normalized:
            raise BridgeError("invalid_payload", "search_text path prefixes must not be empty")
        values.append(validate_repo_relative_path(normalized))
    return tuple(values)


def _extensions(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("extensions", [])
    if not isinstance(raw, list) or len(raw) > 12 or not all(isinstance(item, str) for item in raw):
        raise BridgeError("invalid_payload", "search_text payload.extensions must be a list of at most 12 strings")
    values: list[str] = []
    for item in raw:
        normalized = item.lower()
        if _EXTENSION_RE.fullmatch(normalized) is None:
            raise BridgeError("invalid_payload", f"Unsafe search_text extension: {item}")
        values.append(normalized)
    return tuple(sorted(set(values)))


def _safe_path(root: Path, relative: str) -> Path:
    normalized = validate_repo_relative_path(relative)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    if candidate.is_symlink():
        raise BridgeError("unsafe_path", f"search_text path must not be a symlink: {normalized}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BridgeError("unsafe_path", f"search_text path escaped the repository: {normalized}") from exc
    return resolved


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


def search_repository(config: Any, payload: dict[str, Any]) -> dict[str, Any]:
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

    root = Path(config.fixture_repo_path).expanduser().resolve(strict=True)
    git = Git(root)
    head = git.run(["rev-parse", "HEAD"]).stdout.strip().lower()
    tracked_raw = git.run(["ls-files", "-z"]).stdout
    tracked = [item.replace("\\", "/") for item in tracked_raw.split("\0") if item]
    if len(tracked) > _MAX_TRACKED_FILES:
        tracked = tracked[:_MAX_TRACKED_FILES]
        tracked_truncated = True
    else:
        tracked_truncated = False

    needle = query if case_sensitive else query.casefold()
    matches: list[dict[str, Any]] = []
    total_matches = 0
    scanned_files = 0
    skipped_files = 0

    for relative in tracked:
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

        path = _safe_path(root, normalized)
        if not path.is_file():
            skipped_files += 1
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                skipped_files += 1
                continue
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            skipped_files += 1
            continue
        scanned_files += 1

        path_haystack = normalized if case_sensitive else normalized.casefold()
        if needle in path_haystack:
            total_matches += 1
            if len(matches) < max_results:
                matches.append({"kind": "path", "path": normalized, "line": 0, "text": normalized})

        for number, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.casefold()
            if needle not in haystack:
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
    }
    bounded = _bounded_result(result)
    bounded["returned_matches"] = len(bounded["matches"])
    return bounded
