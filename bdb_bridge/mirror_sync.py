from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import BridgeError, path_matches
from .workspace_manager import Git, changed_paths


MIRROR_STATUS_SCHEMA = "bdb-mirror-sync-v1"
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_HTTPS_GITHUB_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
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


def _safe_branch(value: str, field: str) -> str:
    if (
        _BRANCH_RE.fullmatch(value) is None
        or value.startswith(('/', '.'))
        or value.endswith(('/', '.'))
        or '..' in value
        or '@{' in value
        or '//' in value
        or '\\' in value
    ):
        raise BridgeError("invalid_config", f"{field} is not a safe branch name")
    return value


@dataclass(frozen=True)
class MirrorSyncSettings:
    enabled: bool
    remote_name: str | None
    remote_url: str | None
    local_branch: str | None
    remote_branch: str | None
    timeout_seconds: float

    @classmethod
    def from_config(cls, config: Any) -> "MirrorSyncSettings":
        enabled = bool(getattr(config, "mirror_sync_enabled", False))
        remote_name = getattr(config, "mirror_remote_name", None)
        remote_url = getattr(config, "mirror_remote_url", None)
        local_branch = getattr(config, "mirror_local_branch", None)
        remote_branch = getattr(config, "mirror_remote_branch", None)
        timeout_seconds = float(getattr(config, "mirror_timeout_seconds", 60.0))

        if not enabled:
            return cls(False, None, None, None, None, timeout_seconds)
        if not isinstance(remote_name, str) or _REMOTE_NAME_RE.fullmatch(remote_name) is None:
            raise BridgeError("invalid_config", "mirror_remote_name is invalid")
        if not isinstance(remote_url, str) or _HTTPS_GITHUB_RE.fullmatch(remote_url) is None:
            raise BridgeError(
                "invalid_config",
                "mirror_remote_url must be an exact credential-free HTTPS GitHub .git URL",
            )
        if not isinstance(local_branch, str):
            raise BridgeError("invalid_config", "mirror_local_branch is required")
        if not isinstance(remote_branch, str):
            raise BridgeError("invalid_config", "mirror_remote_branch is required")
        _safe_branch(local_branch, "mirror_local_branch")
        _safe_branch(remote_branch, "mirror_remote_branch")
        if not 5.0 <= timeout_seconds <= 120.0:
            raise BridgeError("invalid_config", "mirror_timeout_seconds must be between 5 and 120")
        return cls(True, remote_name, remote_url, local_branch, remote_branch, timeout_seconds)


class MirrorSynchronizer:
    """Fast-forward-only local checkout to GitHub mirror synchronization."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.settings = MirrorSyncSettings.from_config(config)
        self.source = Path(config.fixture_repo_path).expanduser().resolve(strict=True)
        self.git = Git(self.source)
        self.status_path = Path(config.runtime_dir).expanduser().resolve(strict=False) / "mirror-sync-status.json"

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def sync(self, *, phase: str, expected_head: str | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        settings = self.settings
        assert settings.remote_name is not None
        assert settings.remote_url is not None
        assert settings.local_branch is not None
        assert settings.remote_branch is not None

        started_at = _utc_now()
        local_head: str | None = None
        remote_head_before: str | None = None
        try:
            branch_result = self.git.run(
                ["symbolic-ref", "-q", "--short", "HEAD"],
                check=False,
                timeout=settings.timeout_seconds,
            )
            branch = branch_result.stdout.strip()
            if branch_result.returncode != 0 or branch != settings.local_branch:
                raise BridgeError(
                    "mirror_sync_failed",
                    "Mirror synchronization requires the configured attached local branch",
                )

            local_head = self.git.run(
                ["rev-parse", "HEAD"], timeout=settings.timeout_seconds
            ).stdout.strip().lower()
            if _SHA_RE.fullmatch(local_head) is None:
                raise BridgeError("mirror_sync_failed", "Local mirror HEAD is invalid")
            if expected_head is not None and local_head != expected_head.lower():
                raise BridgeError(
                    "mirror_sync_failed",
                    "Local HEAD differs from the expected post-promotion commit",
                )

            status_text = self.git.run(
                ["status", "--porcelain=v1", "--untracked-files=all"],
                timeout=settings.timeout_seconds,
            ).stdout
            controlled = [
                path
                for path in changed_paths(status_text)
                if path_matches(path, self.config.allowed_paths)
            ]
            if controlled:
                raise BridgeError(
                    "dirty_source_checkout",
                    f"Mirror synchronization found controlled changes: {controlled[:20]}",
                )

            remote_url_result = self.git.run(
                ["remote", "get-url", settings.remote_name],
                check=False,
                timeout=settings.timeout_seconds,
            )
            configured_url = remote_url_result.stdout.strip()
            if remote_url_result.returncode != 0 or configured_url != settings.remote_url:
                raise BridgeError(
                    "mirror_sync_failed",
                    "Configured Git remote is missing or points to an unexpected URL",
                )

            remote_head_before = self._remote_head()
            if remote_head_before == local_head:
                outcome = self._outcome(
                    status="up_to_date",
                    phase=phase,
                    local_head=local_head,
                    remote_head_before=remote_head_before,
                    remote_head_after=remote_head_before,
                    pushed=False,
                    started_at=started_at,
                )
                self._record(outcome)
                return outcome

            push = self.git.run(
                [
                    "push",
                    "--porcelain",
                    settings.remote_name,
                    f"{settings.local_branch}:refs/heads/{settings.remote_branch}",
                ],
                check=False,
                timeout=settings.timeout_seconds,
                env=self._network_env(),
            )
            if push.returncode != 0:
                raise BridgeError(
                    "mirror_sync_failed",
                    "Fast-forward-only mirror push was rejected or unavailable",
                )

            remote_head_after = self._remote_head()
            if remote_head_after != local_head:
                raise BridgeError(
                    "mirror_sync_failed",
                    "Mirror verification did not observe the exact local HEAD",
                )

            outcome = self._outcome(
                status="synced",
                phase=phase,
                local_head=local_head,
                remote_head_before=remote_head_before,
                remote_head_after=remote_head_after,
                pushed=True,
                started_at=started_at,
            )
            self._record(outcome)
            return outcome
        except BridgeError as exc:
            failure = {
                "schema": MIRROR_STATUS_SCHEMA,
                "status": "failed",
                "phase": phase,
                "error_code": str(exc.code),
                "local_head": local_head,
                "remote_head_before": remote_head_before,
                "remote_name": settings.remote_name,
                "local_branch": settings.local_branch,
                "remote_branch": settings.remote_branch,
                "pushed": False,
                "started_at": started_at,
                "completed_at": _utc_now(),
            }
            self._record(failure)
            raise

    def try_sync(self, *, phase: str, expected_head: str | None = None) -> dict[str, Any] | None:
        try:
            return self.sync(phase=phase, expected_head=expected_head)
        except BridgeError as exc:
            current = self.read_status()
            if current is not None and current.get("status") == "failed":
                return current
            return {
                "schema": MIRROR_STATUS_SCHEMA,
                "status": "failed",
                "phase": phase,
                "error_code": str(exc.code),
                "local_head": expected_head,
                "pushed": False,
                "completed_at": _utc_now(),
            }

    def read_status(self) -> dict[str, Any] | None:
        if not self.enabled or not self.status_path.exists():
            return None
        if self.status_path.is_symlink() or not self.status_path.is_file():
            return None
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("schema") != MIRROR_STATUS_SCHEMA:
            return None
        return raw

    def _remote_head(self) -> str | None:
        settings = self.settings
        assert settings.remote_name is not None
        assert settings.remote_branch is not None
        result = self.git.run(
            [
                "ls-remote",
                "--heads",
                settings.remote_name,
                f"refs/heads/{settings.remote_branch}",
            ],
            check=False,
            timeout=settings.timeout_seconds,
            env=self._network_env(),
        )
        if result.returncode != 0:
            raise BridgeError("mirror_sync_failed", "Mirror remote is unavailable")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        if len(lines) != 1:
            raise BridgeError("mirror_sync_failed", "Mirror remote returned an ambiguous branch result")
        parts = lines[0].split()
        if len(parts) != 2 or parts[1] != f"refs/heads/{settings.remote_branch}":
            raise BridgeError("mirror_sync_failed", "Mirror remote returned an invalid branch result")
        sha = parts[0].lower()
        if _SHA_RE.fullmatch(sha) is None:
            raise BridgeError("mirror_sync_failed", "Mirror remote returned an invalid commit SHA")
        return sha

    @staticmethod
    def _network_env() -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "Never"
        return env

    def _outcome(
        self,
        *,
        status: str,
        phase: str,
        local_head: str,
        remote_head_before: str | None,
        remote_head_after: str | None,
        pushed: bool,
        started_at: str,
    ) -> dict[str, Any]:
        settings = self.settings
        return {
            "schema": MIRROR_STATUS_SCHEMA,
            "status": status,
            "phase": phase,
            "local_head": local_head,
            "remote_head_before": remote_head_before,
            "remote_head_after": remote_head_after,
            "remote_name": settings.remote_name,
            "local_branch": settings.local_branch,
            "remote_branch": settings.remote_branch,
            "pushed": pushed,
            "started_at": started_at,
            "completed_at": _utc_now(),
        }

    def _record(self, outcome: dict[str, Any]) -> None:
        try:
            _atomic_json(self.status_path, outcome)
        except OSError:
            # The sync result remains authoritative. A status-cache write failure
            # must not turn a verified push into a false negative.
            pass
