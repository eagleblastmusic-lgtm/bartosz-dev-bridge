from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_LAUNCH_SCHEMA = "bdb-project-launch-v1"
PROJECT_LAUNCH_QUEUE_SCHEMA = "bdb-project-launch-queue-v1"
MAX_PROJECT_PROMPT_CHARS = 50_000
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("timestamp must use canonical UTC Z form")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class ProjectLaunch:
    launch_id: str
    repo_alias: str
    prompt: str
    auto_send: bool
    created_at: str
    expires_at: str
    schema: str = PROJECT_LAUNCH_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "launch_id": self.launch_id,
            "repo_alias": self.repo_alias,
            "prompt": self.prompt,
            "auto_send": self.auto_send,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProjectLaunch":
        if not isinstance(value, dict) or value.get("schema") != PROJECT_LAUNCH_SCHEMA:
            raise ValueError("project launch schema is unsupported")
        launch_id = value.get("launch_id")
        repo_alias = value.get("repo_alias")
        prompt = value.get("prompt")
        auto_send = value.get("auto_send")
        created_at = value.get("created_at")
        expires_at = value.get("expires_at")
        if not isinstance(launch_id, str):
            raise ValueError("launch_id must be a string")
        uuid.UUID(launch_id)
        if not isinstance(repo_alias, str) or _ALIAS_RE.fullmatch(repo_alias) is None:
            raise ValueError("repo_alias has an unsafe format")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROJECT_PROMPT_CHARS:
            raise ValueError("prompt must be non-empty and bounded")
        if not isinstance(auto_send, bool):
            raise ValueError("auto_send must be boolean")
        if not isinstance(created_at, str) or not isinstance(expires_at, str):
            raise ValueError("launch timestamps must be strings")
        created = _parse_utc(created_at)
        expires = _parse_utc(expires_at)
        if expires <= created:
            raise ValueError("expires_at must be later than created_at")
        return cls(
            launch_id=launch_id,
            repo_alias=repo_alias,
            prompt=prompt,
            auto_send=auto_send,
            created_at=created_at,
            expires_at=expires_at,
        )


class ProjectLaunchQueue:
    """A single-item local queue used by Control Center and the browser extension.

    Control Center writes one bounded prompt after the operator explicitly starts a
    project. The extension peeks through Native Messaging and acknowledges only
    after the prompt was inserted (and, when requested, its submission confirmed).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        now_fn: Callable[[], datetime] = _utc_now,
        writer: Callable[[Path, dict[str, Any]], None] = _atomic_json_write,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.now_fn = now_fn
        self._writer = writer

    def enqueue(
        self,
        *,
        repo_alias: str,
        prompt: str,
        auto_send: bool,
        ttl_minutes: int = 10,
    ) -> ProjectLaunch:
        if _ALIAS_RE.fullmatch(repo_alias) is None:
            raise ValueError("repo_alias must match ^[a-z][a-z0-9-]{0,31}$")
        normalized_prompt = prompt.strip()
        if not normalized_prompt or len(normalized_prompt) > MAX_PROJECT_PROMPT_CHARS:
            raise ValueError("prompt must be non-empty and at most 50000 characters")
        if not isinstance(auto_send, bool):
            raise ValueError("auto_send must be boolean")
        if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or not 1 <= ttl_minutes <= 60:
            raise ValueError("ttl_minutes must be an integer between 1 and 60")
        now = self.now_fn().astimezone(timezone.utc)
        launch = ProjectLaunch(
            launch_id=str(uuid.uuid4()),
            repo_alias=repo_alias,
            prompt=normalized_prompt,
            auto_send=auto_send,
            created_at=_utc_text(now),
            expires_at=_utc_text(now + timedelta(minutes=ttl_minutes)),
        )
        self._write(launch)
        return launch

    def peek(self) -> ProjectLaunch | None:
        launch = self._read()
        if launch is None:
            return None
        if self.now_fn().astimezone(timezone.utc) >= _parse_utc(launch.expires_at):
            self._write(None)
            return None
        return launch

    def acknowledge(self, launch_id: str) -> bool:
        try:
            uuid.UUID(launch_id)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("launch_id must be a UUID") from None
        launch = self.peek()
        if launch is None or launch.launch_id != launch_id:
            return False
        self._write(None)
        return True

    def _read(self) -> ProjectLaunch | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("project launch queue must be a regular file")
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("schema") != PROJECT_LAUNCH_QUEUE_SCHEMA:
            raise ValueError("project launch queue schema is unsupported")
        pending = raw.get("pending")
        return None if pending is None else ProjectLaunch.from_dict(pending)

    def _write(self, launch: ProjectLaunch | None) -> None:
        self._writer(
            self.path,
            {
                "schema": PROJECT_LAUNCH_QUEUE_SCHEMA,
                "pending": None if launch is None else launch.to_dict(),
            },
        )
