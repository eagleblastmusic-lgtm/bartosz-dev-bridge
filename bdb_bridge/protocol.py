from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import Any, Iterable

SCHEMA_VERSION = "1.1"
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


def result_path_for(session_id: str, sequence: int) -> str:
    validate_session_id(session_id)
    return f"sessions/{session_id}/results/{sequence:06d}.json"
