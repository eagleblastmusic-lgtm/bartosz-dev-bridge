from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable

SCHEMA_VERSION = "1.1"
EXECUTOR_VERSION = "0.1.0-poc"
MAX_RESULT_BYTES = 16 * 1024
MAX_TAIL_CHARS = 5_000
MAX_READ_LINES = 400
SESSION_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
)
COMMAND_PATH_RE = re.compile(
    r"^sessions/(?P<session>[^/]+)/commands/(?P<sequence>[0-9]{6})\.json$"
)


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_session_id(value: str) -> None:
    if not SESSION_RE.fullmatch(value):
        raise BridgeError("invalid_session_id", "session_id must be UUID or ULID")


def validate_repo_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("./") or "\\" in value or "\x00" in value:
        raise BridgeError("unsafe_path", "Path must be a non-empty repository-relative POSIX path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise BridgeError("unsafe_path", f"Unsafe repository path: {value}")
    return pure.as_posix()


def validate_path_pattern(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise BridgeError("unsafe_path", f"Unsafe allowed path pattern: {value}")
    if any(part == ".." for part in PurePosixPath(value).parts):
        raise BridgeError("unsafe_path", f"Unsafe allowed path pattern: {value}")


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def require_string(mapping: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise BridgeError("invalid_payload", f"{key} must be a string")
    return value


def require_int(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int):
        raise BridgeError("invalid_payload", f"{key} must be an integer")
    return value


def sanitized_test_environment() -> dict[str, str]:
    allowed = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
    return env


def changed_paths(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value.replace("\\", "/"))
    return sorted(paths)


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def result_path_for(session_id: str, sequence: int) -> str:
    validate_session_id(session_id)
    return f"sessions/{session_id}/results/{sequence:06d}.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def tail(value: str, limit: int = MAX_TAIL_CHARS) -> str:
    return value[-limit:]


def text_or_empty(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def summary_from_test(outcome: dict[str, Any]) -> str:
    combined = (outcome["stdout"] + "\n" + outcome["stderr"]).strip().splitlines()
    if combined:
        return tail(combined[-1], 500)
    return f"pytest exit code {outcome['exit_code']}"


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def finalize_result(result: dict[str, Any]) -> str:
    candidate = dict(result)
    was_truncated = bool(candidate.get("truncated", False))
    for field in ("stdout_tail", "stderr_tail", "diff"):
        if field in candidate:
            original = str(candidate[field])
            if len(original) > MAX_TAIL_CHARS:
                was_truncated = True
            candidate[field] = tail(original, MAX_TAIL_CHARS)
    if isinstance(candidate.get("data"), dict) and "content" in candidate["data"]:
        candidate["data"] = dict(candidate["data"])
        original_content = str(candidate["data"]["content"])
        if len(original_content) > 8_000:
            was_truncated = True
        candidate["data"]["content"] = tail(original_content, 8_000)
    candidate["truncated"] = was_truncated

    while True:
        without_marker = dict(candidate)
        without_marker.pop("end_marker", None)
        digest = hashlib.sha256(canonical_json(without_marker).encode("utf-8")).hexdigest()
        candidate["end_marker"] = f"BDB-END:sha256:{digest}"
        serialized = json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2)
        if len(serialized.encode("utf-8")) <= MAX_RESULT_BYTES:
            return serialized
        candidate["truncated"] = True
        shrunk = False
        for field in ("diff", "stdout_tail", "stderr_tail"):
            if len(str(candidate.get(field, ""))) > 1_000:
                candidate[field] = tail(str(candidate[field]), max(1_000, len(str(candidate[field])) // 2))
                shrunk = True
        data = candidate.get("data")
        if isinstance(data, dict) and len(str(data.get("content", ""))) > 1_000:
            data["content"] = tail(str(data["content"]), max(1_000, len(str(data["content"])) // 2))
            shrunk = True
        if not shrunk:
            raise BridgeError("result_too_large", "Unable to fit result into 16 KiB")
