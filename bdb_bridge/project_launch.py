from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


PROJECT_LAUNCH_SCHEMA = "bdb-project-launch-v1"
PROJECT_LAUNCH_QUEUE_SCHEMA = "bdb-project-launch-queue-v1"
PROJECT_LAUNCH_CLAIM_SCHEMA = "bdb-project-launch-claim-v1"
MAX_PROJECT_PROMPT_CHARS = 50_000
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_LOCK_TIMEOUT_SECONDS = 3.0
_STALE_LOCK_SECONDS = 30.0


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


def _valid_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID string")
    try:
        uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f"{field} must be a UUID string") from error
    return value


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
        launch_id = _valid_uuid(value.get("launch_id"), "launch_id")
        repo_alias = value.get("repo_alias")
        prompt = value.get("prompt")
        auto_send = value.get("auto_send")
        created_at = value.get("created_at")
        expires_at = value.get("expires_at")
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


@dataclass(frozen=True)
class ProjectLaunchClaim:
    claim_id: str
    launch_id: str
    claimed_at: str
    expires_at: str
    schema: str = PROJECT_LAUNCH_CLAIM_SCHEMA

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "claim_id": self.claim_id,
            "launch_id": self.launch_id,
            "claimed_at": self.claimed_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProjectLaunchClaim":
        if not isinstance(value, dict) or value.get("schema") != PROJECT_LAUNCH_CLAIM_SCHEMA:
            raise ValueError("project launch claim schema is unsupported")
        claim_id = _valid_uuid(value.get("claim_id"), "claim_id")
        launch_id = _valid_uuid(value.get("launch_id"), "launch_id")
        claimed_at = value.get("claimed_at")
        expires_at = value.get("expires_at")
        if not isinstance(claimed_at, str) or not isinstance(expires_at, str):
            raise ValueError("claim timestamps must be strings")
        claimed = _parse_utc(claimed_at)
        expires = _parse_utc(expires_at)
        if expires <= claimed:
            raise ValueError("claim expires_at must be later than claimed_at")
        return cls(claim_id, launch_id, claimed_at, expires_at)


class ProjectLaunchQueue:
    """Single pending prompt with a cross-process lease for exactly one browser tab."""

    def __init__(
        self,
        path: str | Path,
        *,
        now_fn: Callable[[], datetime] = _utc_now,
        writer: Callable[[Path, dict[str, Any]], None] = _atomic_json_write,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
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
        with self._lock():
            pending, claim = self._read_state_unlocked()
            pending, claim = self._normalize_expiry(pending, claim)
            if pending is not None:
                raise ValueError("project launch queue already contains a pending prompt")
            now = self.now_fn().astimezone(timezone.utc)
            launch = ProjectLaunch(
                launch_id=str(uuid.uuid4()),
                repo_alias=repo_alias,
                prompt=normalized_prompt,
                auto_send=auto_send,
                created_at=_utc_text(now),
                expires_at=_utc_text(now + timedelta(minutes=ttl_minutes)),
            )
            self._write_state_unlocked(launch, None)
            return launch

    def peek(self) -> ProjectLaunch | None:
        with self._lock():
            pending, claim = self._read_state_unlocked()
            normalized_pending, normalized_claim = self._normalize_expiry(pending, claim)
            if (normalized_pending, normalized_claim) != (pending, claim):
                self._write_state_unlocked(normalized_pending, normalized_claim)
            return normalized_pending

    def claim(
        self,
        *,
        launch_id: str,
        claim_id: str,
        lease_seconds: int = 30,
    ) -> ProjectLaunch | None:
        _valid_uuid(launch_id, "launch_id")
        _valid_uuid(claim_id, "claim_id")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 120:
            raise ValueError("lease_seconds must be an integer between 5 and 120")
        with self._lock():
            pending, current_claim = self._read_state_unlocked()
            pending, current_claim = self._normalize_expiry(pending, current_claim)
            if pending is None or pending.launch_id != launch_id:
                self._write_state_unlocked(pending, current_claim)
                return None
            if current_claim is not None:
                if current_claim.claim_id == claim_id and current_claim.launch_id == launch_id:
                    return pending
                return None
            now = self.now_fn().astimezone(timezone.utc)
            claim = ProjectLaunchClaim(
                claim_id=claim_id,
                launch_id=launch_id,
                claimed_at=_utc_text(now),
                expires_at=_utc_text(now + timedelta(seconds=lease_seconds)),
            )
            self._write_state_unlocked(pending, claim)
            return pending

    def acknowledge(self, launch_id: str, claim_id: str) -> bool:
        _valid_uuid(launch_id, "launch_id")
        _valid_uuid(claim_id, "claim_id")
        with self._lock():
            pending, claim = self._read_state_unlocked()
            pending, claim = self._normalize_expiry(pending, claim)
            if (
                pending is None
                or claim is None
                or pending.launch_id != launch_id
                or claim.launch_id != launch_id
                or claim.claim_id != claim_id
            ):
                self._write_state_unlocked(pending, claim)
                return False
            self._write_state_unlocked(None, None)
            return True

    def _normalize_expiry(
        self,
        pending: ProjectLaunch | None,
        claim: ProjectLaunchClaim | None,
    ) -> tuple[ProjectLaunch | None, ProjectLaunchClaim | None]:
        now = self.now_fn().astimezone(timezone.utc)
        if pending is not None and now >= _parse_utc(pending.expires_at):
            return None, None
        if claim is not None and (
            pending is None
            or claim.launch_id != pending.launch_id
            or now >= _parse_utc(claim.expires_at)
        ):
            claim = None
        return pending, claim

    def _read_state_unlocked(self) -> tuple[ProjectLaunch | None, ProjectLaunchClaim | None]:
        if not self.path.exists():
            return None, None
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("project launch queue must be a regular file")
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("schema") != PROJECT_LAUNCH_QUEUE_SCHEMA:
            raise ValueError("project launch queue schema is unsupported")
        pending_raw = raw.get("pending")
        claim_raw = raw.get("claim")
        pending = None if pending_raw is None else ProjectLaunch.from_dict(pending_raw)
        claim = None if claim_raw is None else ProjectLaunchClaim.from_dict(claim_raw)
        return pending, claim

    def _write_state_unlocked(
        self,
        launch: ProjectLaunch | None,
        claim: ProjectLaunchClaim | None,
    ) -> None:
        self._writer(
            self.path,
            {
                "schema": PROJECT_LAUNCH_QUEUE_SCHEMA,
                "pending": None if launch is None else launch.to_dict(),
                "claim": None if claim is None else claim.to_dict(),
            },
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                    if age >= _STALE_LOCK_SECONDS:
                        self.lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("project launch queue lock timed out")
                time.sleep(0.01)
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
