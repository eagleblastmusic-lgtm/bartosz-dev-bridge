from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bdb_bridge import BridgeConfig, Journal

NOW = "2026-07-16T01:30:00Z"
REPO_ID = "relationship-fixture"

TOOLS = '''def decorator(fn):
    return fn


def helper(value=1):
    """Return a stable helper value."""
    return value


class Base:
    pass


class Worker:
    def local(self):
        return 1

    def run(self):
        return self.local()
'''

SERVICE = '''import os
import pkg.tools as tools
from .tools import Base, helper as h


def caller():
    h()
    tools.helper()
    return recursive()


def recursive():
    return recursive()


def local():
    return 3


def shadow(helper):
    return helper()


@tools.decorator
class Child(Base):
    def local(self):
        return 2

    def run(self):
        return self.local()

    @classmethod
    def run_via_cls(cls):
        return cls.local()

    def run_unqualified(self):
        return local()


def unknown(obj):
    return obj.missing()
'''

CYCLE_A = '''from .cycle_b import b


def a():
    return b()
'''

CYCLE_B = '''from .cycle_a import a


def b():
    return a()
'''

SIDE_EFFECT = '''raise SystemExit("relationship analyzer executed source")


def dormant():
    return 1
'''


def git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                               text=True, check=False, shell=False)
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "core.autocrlf", "false")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    return path


def write_text(repo: Path, relative: str, value: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8", newline="\n")


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_relationship_fixture(root: Path):
    fixture = init_repo(root / "fixture")
    write_text(fixture, "pkg/__init__.py", "from .tools import helper\n")
    write_text(fixture, "pkg/tools.py", TOOLS)
    write_text(fixture, "pkg/service.py", SERVICE)
    write_text(fixture, "pkg/cycle_a.py", CYCLE_A)
    write_text(fixture, "pkg/cycle_b.py", CYCLE_B)
    write_text(fixture, "pkg/side_effect.py", SIDE_EFFECT)
    commit1 = commit_all(fixture, "relationship baseline")
    write_text(fixture, "pkg/extra.py", "from .tools import helper\n\ndef extra():\n    return helper()\n")
    commit2 = commit_all(fixture, "add extra relationship")
    control = root / "control"
    control.mkdir(parents=True)
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    cfg = BridgeConfig(
        control, fixture, root / "worktrees", repository_id=REPO_ID,
        runtime_dir=runtime, journal_path=runtime / "journal.db",
        allowed_paths=("pkg/service.py",), python_executable=sys.executable,
        test_timeout_seconds=20,
    )
    journal = Journal.open(cfg.journal_path, now_fn=lambda: NOW)
    return cfg, journal, fixture, {"commit1": commit1, "commit2": commit2}


def write_config(root: Path, cfg: BridgeConfig) -> Path:
    target = root / "config.json"
    target.write_text(json.dumps({
        "schema_version": "1.1", "control_repo_path": str(cfg.control_repo_path),
        "fixture_repo_path": str(cfg.fixture_repo_path), "worktree_root": str(cfg.worktree_root),
        "runtime_dir": str(cfg.runtime_dir), "journal_path": str(cfg.journal_path),
        "repository_id": cfg.repository_id, "allowed_paths": list(cfg.allowed_paths),
        "commands_ref": cfg.commands_ref, "results_ref": cfg.results_ref,
        "python_executable": sys.executable, "heartbeat_interval_seconds": 0.05,
        "heartbeat_stale_seconds": 2.0, "idle_poll_seconds": 0.05,
    }, sort_keys=True, separators=(",", ":")), encoding="utf-8", newline="\n")
    return target
