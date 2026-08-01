from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .protocol import BridgeError, SCHEMA_VERSION


@dataclass(frozen=True)
class BridgeConfig:
    control_repo_path: Path
    fixture_repo_path: Path
    worktree_root: Path
    repository_id: str = "bdb-poc-fixture"
    allowed_paths: tuple[str, ...] = ("src/clamp.py", "tests/test_clamp.py")
    commands_ref: str = "origin/commands"
    results_ref: str = "origin/results"
    poll_interval_seconds: float = 5.0
    max_poll_seconds: float = 300.0
    max_sequence: int = 3
    test_timeout_seconds: float = 45.0
    python_executable: str = sys.executable
    runtime_dir: Path | None = None
    journal_path: Path | None = None
    heartbeat_interval_seconds: float = 1.0
    heartbeat_stale_seconds: float = 10.0
    idle_poll_seconds: float = 1.0
    direct_spool_enabled: bool = True
    direct_spool_dir: Path | None = None
    direct_result_dir: Path | None = None
    workspace_mode: str = "isolated_worktree"
    mirror_sync_enabled: bool = False
    mirror_remote_name: str | None = None
    mirror_remote_url: str | None = None
    mirror_local_branch: str | None = None
    mirror_remote_branch: str | None = None
    mirror_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        c_repo = Path(self.control_repo_path).expanduser().resolve(strict=False)
        f_repo = Path(self.fixture_repo_path).expanduser().resolve(strict=False)
        w_root = Path(self.worktree_root).expanduser().resolve(strict=False)

        object.__setattr__(self, "control_repo_path", c_repo)
        object.__setattr__(self, "fixture_repo_path", f_repo)
        object.__setattr__(self, "worktree_root", w_root)

        r_dir = self.runtime_dir
        if r_dir is None:
            r_dir = w_root.parent / "bdb_runtime"
        r_dir = Path(r_dir).expanduser().resolve(strict=False)
        object.__setattr__(self, "runtime_dir", r_dir)

        j_path = self.journal_path
        if j_path is None:
            j_path = r_dir / "journal.db"
        j_path = Path(j_path).expanduser().resolve(strict=False)
        object.__setattr__(self, "journal_path", j_path)

        if not isinstance(self.direct_spool_enabled, bool):
            raise BridgeError("invalid_config", "direct_spool_enabled must be a boolean")
        spool_dir = self.direct_spool_dir
        if spool_dir is None:
            spool_dir = r_dir / "direct_spool" / "inbox"
        spool_dir = Path(spool_dir).expanduser().resolve(strict=False)
        object.__setattr__(self, "direct_spool_dir", spool_dir)

        result_dir = self.direct_result_dir
        if result_dir is None:
            result_dir = r_dir / "direct_spool" / "results"
        result_dir = Path(result_dir).expanduser().resolve(strict=False)
        object.__setattr__(self, "direct_result_dir", result_dir)

        if self.workspace_mode not in {"isolated_worktree", "direct_checkout"}:
            raise BridgeError(
                "invalid_config",
                "workspace_mode must be isolated_worktree or direct_checkout",
            )

        if not isinstance(self.mirror_sync_enabled, bool):
            raise BridgeError("invalid_config", "mirror_sync_enabled must be a boolean")
        if not 5.0 <= float(self.mirror_timeout_seconds) <= 120.0:
            raise BridgeError(
                "invalid_config",
                "mirror_timeout_seconds must be between 5 and 120",
            )
        if self.mirror_sync_enabled:
            from .mirror_sync import MirrorSyncSettings

            MirrorSyncSettings.from_config(self)

        for name, val in [
            ("poll_interval_seconds", self.poll_interval_seconds),
            ("max_poll_seconds", self.max_poll_seconds),
            ("test_timeout_seconds", self.test_timeout_seconds),
            ("heartbeat_interval_seconds", self.heartbeat_interval_seconds),
            ("heartbeat_stale_seconds", self.heartbeat_stale_seconds),
            ("idle_poll_seconds", self.idle_poll_seconds),
        ]:
            if val <= 0:
                raise BridgeError("invalid_config", f"{name} must be positive, got {val}")

        if self.heartbeat_stale_seconds <= self.heartbeat_interval_seconds:
            raise BridgeError(
                "invalid_config",
                f"heartbeat_stale_seconds ({self.heartbeat_stale_seconds}) must be greater than heartbeat_interval_seconds ({self.heartbeat_interval_seconds})",
            )

        def comparable_path(path: Path) -> str:
            value = str(path)
            if sys.platform == "win32":
                if value.startswith("\\\\?\\UNC\\"):
                    value = "\\\\" + value[8:]
                elif value.startswith("\\\\?\\"):
                    value = value[4:]
            return os.path.normcase(os.path.normpath(value))

        def is_subpath(p1: Path, p2: Path) -> bool:
            child = comparable_path(p1)
            parent = comparable_path(p2)
            try:
                return os.path.commonpath((child, parent)) == parent
            except ValueError:
                return False

        if is_subpath(r_dir, c_repo) or is_subpath(r_dir, f_repo) or is_subpath(r_dir, w_root):
            raise BridgeError(
                "invalid_config",
                f"runtime_dir ({r_dir}) cannot alias or overlap with control_repo ({c_repo}), fixture_repo ({f_repo}), or worktree_root ({w_root})",
            )
        for name, path in (
            ("direct_spool_dir", spool_dir),
            ("direct_result_dir", result_dir),
        ):
            if not is_subpath(path, r_dir):
                raise BridgeError(
                    "invalid_config",
                    f"{name} ({path}) must be contained within runtime_dir ({r_dir})",
                )
            if path == r_dir or path == j_path:
                raise BridgeError("invalid_config", f"{name} must be a dedicated directory")
        if spool_dir == result_dir or is_subpath(spool_dir, result_dir) or is_subpath(result_dir, spool_dir):
            raise BridgeError(
                "invalid_config",
                "direct_spool_dir and direct_result_dir must not overlap",
            )

    @classmethod
    def from_json(cls, path: Path) -> "BridgeConfig":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise BridgeError("unsupported_schema", "Local config schema_version must be 1.1")
        allowed = raw.get("allowed_paths", ["src/clamp.py", "tests/test_clamp.py"])
        if not isinstance(allowed, list) or not allowed or not all(isinstance(v, str) for v in allowed):
            raise BridgeError("invalid_config", "allowed_paths must be a non-empty string list")

        commands_ref = str(raw.get("commands_ref") or "origin/commands")
        results_ref = str(raw.get("results_ref") or "origin/results")
        runtime_dir = Path(raw["runtime_dir"]).expanduser().resolve() if "runtime_dir" in raw else None
        journal_path = Path(raw["journal_path"]).expanduser().resolve() if "journal_path" in raw else None
        direct_spool_dir = (
            Path(raw["direct_spool_dir"]).expanduser().resolve()
            if "direct_spool_dir" in raw
            else None
        )
        direct_result_dir = (
            Path(raw["direct_result_dir"]).expanduser().resolve()
            if "direct_result_dir" in raw
            else None
        )
        direct_spool_enabled = raw.get("direct_spool_enabled", True)
        if not isinstance(direct_spool_enabled, bool):
            raise BridgeError("invalid_config", "direct_spool_enabled must be a boolean")

        return cls(
            control_repo_path=Path(raw["control_repo_path"]).expanduser().resolve(),
            fixture_repo_path=Path(raw["fixture_repo_path"]).expanduser().resolve(),
            worktree_root=Path(raw["worktree_root"]).expanduser().resolve(),
            repository_id=str(raw.get("repository_id", "bdb-poc-fixture")),
            allowed_paths=tuple(allowed),
            commands_ref=commands_ref,
            results_ref=results_ref,
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 5.0)),
            max_poll_seconds=float(raw.get("max_poll_seconds", 300.0)),
            max_sequence=int(raw.get("max_sequence", 3)),
            test_timeout_seconds=float(raw.get("test_timeout_seconds", 45.0)),
            python_executable=str(raw.get("python_executable") or sys.executable),
            runtime_dir=runtime_dir,
            journal_path=journal_path,
            heartbeat_interval_seconds=float(raw.get("heartbeat_interval_seconds", 1.0)),
            heartbeat_stale_seconds=float(raw.get("heartbeat_stale_seconds", 10.0)),
            idle_poll_seconds=float(raw.get("idle_poll_seconds", 1.0)),
            direct_spool_enabled=direct_spool_enabled,
            direct_spool_dir=direct_spool_dir,
            direct_result_dir=direct_result_dir,
            workspace_mode=str(raw.get("workspace_mode") or "isolated_worktree"),
            mirror_sync_enabled=raw.get("mirror_sync_enabled", False),
            mirror_remote_name=raw.get("mirror_remote_name"),
            mirror_remote_url=raw.get("mirror_remote_url"),
            mirror_local_branch=raw.get("mirror_local_branch"),
            mirror_remote_branch=raw.get("mirror_remote_branch"),
            mirror_timeout_seconds=float(raw.get("mirror_timeout_seconds", 60.0)),
        )
