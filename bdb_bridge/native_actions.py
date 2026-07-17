from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import BridgeConfig
from .git_object_reader import GitObjectReader
from .local_spool_transport import LOCAL_ENVELOPE_SCHEMA
from .protocol import (
    BridgeError,
    SCHEMA_VERSION,
    command_id_for,
    require_int,
    require_string,
    validate_base_sha,
    validate_session_id,
)


ACTION_SCHEMA = "bdb-action-v1"
SESSION_STORE_SCHEMA = "bdb-native-session-store-v1"
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_SUPPORTED_OPERATIONS = frozenset(
    {
        "open_read",
        "replace_exact_and_test",
        "multi_file_patch",
    }
)
_MAX_SESSION_RECORDS = 1000
_DEFAULT_TTL_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RepositoryAlias:
    alias: str
    bridge_config_path: Path
    bridge_config: BridgeConfig

    @classmethod
    def load(cls, alias: str, bridge_config_path: str | Path) -> "RepositoryAlias":
        if _ALIAS_RE.fullmatch(alias) is None:
            raise BridgeError("invalid_config", f"Unsafe repository alias: {alias}")
        path = Path(bridge_config_path).expanduser().resolve(strict=True)
        if path.is_symlink() or not path.is_file():
            raise BridgeError("invalid_config", f"Repository alias {alias} must reference a regular config file")
        return cls(alias, path, BridgeConfig.from_json(path))


@dataclass(frozen=True)
class NativeSessionRecord:
    session_id: str
    repo_alias: str
    repository_id: str
    base_sha: str
    created_at: str


class NativeSessionStore:
    """Durably bind a BDB session to one trusted alias and exact base SHA."""

    def __init__(self, path: str | Path, *, writer: Callable[[Path, dict[str, Any]], None]) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self._writer = writer

    def get(self, session_id: str) -> NativeSessionRecord | None:
        validate_session_id(session_id)
        raw = self._read()
        item = raw["sessions"].get(session_id)
        if item is None:
            return None
        if not isinstance(item, dict):
            raise BridgeError("invalid_config", "Native session store contains an invalid record")
        return NativeSessionRecord(
            session_id=session_id,
            repo_alias=require_string(item, "repo_alias"),
            repository_id=require_string(item, "repository_id"),
            base_sha=validate_base_sha(require_string(item, "base_sha")),
            created_at=require_string(item, "created_at"),
        )

    def bind(self, record: NativeSessionRecord) -> NativeSessionRecord:
        validate_session_id(record.session_id)
        validate_base_sha(record.base_sha)
        raw = self._read()
        sessions = raw["sessions"]
        existing = sessions.get(record.session_id)
        candidate = {
            "repo_alias": record.repo_alias,
            "repository_id": record.repository_id,
            "base_sha": record.base_sha,
            "created_at": record.created_at,
        }
        if existing is not None:
            if existing != candidate:
                raise BridgeError("journal_conflict", "Native session identity collision")
            return record
        if len(sessions) >= _MAX_SESSION_RECORDS:
            raise BridgeError("invalid_config", "Native session store is full")
        sessions[record.session_id] = candidate
        self._writer(self.path, raw)
        return record

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": SESSION_STORE_SCHEMA, "sessions": {}}
        if self.path.is_symlink() or not self.path.is_file():
            raise BridgeError("invalid_config", "Native session store must be a regular file")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise BridgeError("invalid_config", "Native session store is invalid JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema") != SESSION_STORE_SCHEMA:
            raise BridgeError("unsupported_schema", "Native session store schema is unsupported")
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict) or len(sessions) > _MAX_SESSION_RECORDS:
            raise BridgeError("invalid_config", "Native session store has an invalid sessions map")
        return raw


class NativeActionComposer:
    def __init__(
        self,
        repositories: dict[str, RepositoryAlias],
        session_store: NativeSessionStore,
        *,
        now_fn: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not repositories:
            raise BridgeError("invalid_config", "At least one repository alias is required")
        self.repositories = dict(repositories)
        self.session_store = session_store
        self.now_fn = now_fn

    def context(self, alias: str) -> dict[str, Any]:
        repository = self._repository(alias)
        reader = GitObjectReader(repository.bridge_config.fixture_repo_path)
        reader.ensure_repository()
        base_sha = reader.resolve_commit("HEAD")
        return {
            "repo_alias": alias,
            "repository_id": repository.bridge_config.repository_id,
            "base_sha": base_sha,
            "allowed_paths": list(repository.bridge_config.allowed_paths),
            "max_sequence": repository.bridge_config.max_sequence,
        }

    def compose(self, action: dict[str, Any]) -> tuple[RepositoryAlias, dict[str, Any]]:
        if not isinstance(action, dict) or action.get("schema") != ACTION_SCHEMA:
            raise BridgeError("unsupported_schema", f"Action must use {ACTION_SCHEMA}")
        repo_alias = require_string(action, "repo_alias")
        repository = self._repository(repo_alias)
        operation = require_string(action, "operation")
        if operation not in _SUPPORTED_OPERATIONS:
            raise BridgeError("policy_denied", f"Unsupported native action operation: {operation}")
        payload = action.get("payload")
        if not isinstance(payload, dict):
            raise BridgeError("invalid_payload", "Action payload must be an object")

        supplied_session_id = action.get("session_id")
        if supplied_session_id is None:
            session_id = str(uuid.uuid4())
        else:
            if not isinstance(supplied_session_id, str):
                raise BridgeError("invalid_payload", "session_id must be a string or null")
            validate_session_id(supplied_session_id)
            session_id = supplied_session_id

        sequence = action.get("sequence", 1)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise BridgeError("invalid_payload", "sequence must be a positive integer")
        if sequence > repository.bridge_config.max_sequence:
            raise BridgeError("policy_denied", "sequence exceeds the configured maximum")

        expected_revision = action.get("expected_revision", 0)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise BridgeError("invalid_payload", "expected_revision must be a non-negative integer")
        expected_state_hash = action.get("expected_state_hash")
        if expected_state_hash is not None and not isinstance(expected_state_hash, str):
            raise BridgeError("invalid_payload", "expected_state_hash must be a string or null")

        existing = self.session_store.get(session_id)
        if existing is None:
            if sequence != 1:
                raise BridgeError("invalid_payload", "A new native session must begin at sequence 1")
            reader = GitObjectReader(repository.bridge_config.fixture_repo_path)
            reader.ensure_repository()
            base_sha = reader.resolve_commit("HEAD")
            created_at = _utc_text(self.now_fn())
            session_record = self.session_store.bind(
                NativeSessionRecord(
                    session_id=session_id,
                    repo_alias=repo_alias,
                    repository_id=repository.bridge_config.repository_id,
                    base_sha=base_sha,
                    created_at=created_at,
                )
            )
        else:
            if existing.repo_alias != repo_alias or existing.repository_id != repository.bridge_config.repository_id:
                raise BridgeError("policy_denied", "Session is bound to a different repository alias")
            session_record = existing
            created_at = _utc_text(self.now_fn())

        expires_at = _utc_text(self.now_fn() + timedelta(seconds=_DEFAULT_TTL_SECONDS))
        command = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "command_id": command_id_for(session_id, sequence),
            "sequence": sequence,
            "operation": operation,
            "created_at": created_at,
            "expires_at": expires_at,
            "expected_revision": expected_revision,
            "expected_state_hash": expected_state_hash,
            "payload": payload,
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "repository_id": session_record.repository_id,
            "base_sha": session_record.base_sha,
            "created_at": session_record.created_at,
            "expires_at": expires_at,
        }
        envelope = {
            "schema": LOCAL_ENVELOPE_SCHEMA,
            "submitted_at": created_at,
            "nonce": secrets.token_hex(16),
            "manifest": manifest,
            "command": command,
        }
        return repository, envelope

    def _repository(self, alias: str) -> RepositoryAlias:
        if _ALIAS_RE.fullmatch(alias) is None:
            raise BridgeError("policy_denied", "Repository alias has an unsafe format")
        repository = self.repositories.get(alias)
        if repository is None:
            raise BridgeError("policy_denied", "Repository alias is not configured")
        return repository
