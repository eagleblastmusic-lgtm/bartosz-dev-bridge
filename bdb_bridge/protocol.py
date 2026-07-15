from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable

SCHEMA_VERSION = "1.1"
SESSION_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
)
COMMAND_PATH_RE = re.compile(
    r"^sessions/(?P<session>[^/]+)/commands/(?P<sequence>[0-9]{6})\.json$"
)
MANIFEST_PATH_RE = re.compile(
    r"^sessions/(?P<session>[^/]+)/manifest\.json$"
)
BASE_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


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


def result_path_for(session_id: str, sequence: int) -> str:
    validate_session_id(session_id)
    return f"sessions/{session_id}/results/{sequence:06d}.json"


def manifest_path_for(session_id: str) -> str:
    validate_session_id(session_id)
    return f"sessions/{session_id}/manifest.json"


def command_path_for(session_id: str, sequence: int) -> str:
    validate_session_id(session_id)
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise BridgeError("invalid_payload", "sequence must be a positive integer")
    return f"sessions/{session_id}/commands/{sequence:06d}.json"


def command_id_for(session_id: str, sequence: int) -> str:
    return f"{session_id}:{sequence:06d}"


def parse_command_path(path: str) -> tuple[str, int]:
    validate_repo_relative_path(path)
    match = COMMAND_PATH_RE.fullmatch(path)
    if match is None:
        raise BridgeError("unsafe_path", f"Not a command path: {path}")
    session_id = match.group("session")
    validate_session_id(session_id)
    sequence = int(match.group("sequence"))
    return session_id, sequence


def parse_manifest_path(path: str) -> str:
    validate_repo_relative_path(path)
    match = MANIFEST_PATH_RE.fullmatch(path)
    if match is None:
        raise BridgeError("unsafe_path", f"Not a manifest path: {path}")
    session_id = match.group("session")
    validate_session_id(session_id)
    return session_id


def validate_base_sha(value: str) -> str:
    if not isinstance(value, str) or not BASE_SHA_RE.fullmatch(value):
        raise BridgeError("invalid_base_sha", "base_sha must be a 40-character hex SHA")
    return value.lower()


def parse_strict_utc_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BridgeError("invalid_payload", f"{field} must be a strict UTC ISO-8601 timestamp ending with Z")
    normalized = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BridgeError("invalid_payload", f"{field} must be a valid UTC ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise BridgeError("invalid_payload", f"{field} must include UTC timezone")
    return parsed.astimezone(timezone.utc)


def validate_strict_utc_timestamp(value: str, *, field: str) -> str:
    parse_strict_utc_timestamp(value, field=field)
    return value
