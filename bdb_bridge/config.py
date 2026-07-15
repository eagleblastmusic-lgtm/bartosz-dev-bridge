from __future__ import annotations

import json
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

    @classmethod
    def from_json(cls, path: Path) -> "BridgeConfig":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise BridgeError("unsupported_schema", "Local config schema_version must be 1.1")
        allowed = raw.get("allowed_paths", ["src/clamp.py", "tests/test_clamp.py"])
        if not isinstance(allowed, list) or not allowed or not all(isinstance(v, str) for v in allowed):
            raise BridgeError("invalid_config", "allowed_paths must be a non-empty string list")
        return cls(
            control_repo_path=Path(raw["control_repo_path"]).expanduser().resolve(),
            fixture_repo_path=Path(raw["fixture_repo_path"]).expanduser().resolve(),
            worktree_root=Path(raw["worktree_root"]).expanduser().resolve(),
            repository_id=str(raw.get("repository_id", "bdb-poc-fixture")),
            allowed_paths=tuple(allowed),
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 5.0)),
            max_poll_seconds=float(raw.get("max_poll_seconds", 300.0)),
            max_sequence=int(raw.get("max_sequence", 3)),
            test_timeout_seconds=float(raw.get("test_timeout_seconds", 45.0)),
            python_executable=str(raw.get("python_executable") or sys.executable),
        )
