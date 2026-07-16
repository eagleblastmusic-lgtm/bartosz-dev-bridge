from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bdb_bridge import BridgeConfig, Journal

NOW = "2026-07-16T00:00:00Z"
REPO_ID = "bdb-poc-fixture"

SAMPLE_PY = b'''"""Module docstring."""

def decorator(fn):
    return fn


def module_function(a, b=1, *args, flag=True, **kwargs):
    """First summary line.

    More detail.
    """
    if flag:
        async def nested_async():
            return a

    def nested():
        return a

    return nested()


if True:
    def guarded_function():
        return 1


async def module_async(x: int) -> int:
    return x


class Outer:
    """Outer class."""

    def method(self, value):
        return value

    async def async_method(self):
        return 1

    if True:
        def guarded_method(self):
            return 3

    class Inner:
        def inner_method(self):
            return 2


@decorator
def decorated(value):
    return value
'''


def git(repo: Path, *args: str, check: bool = True, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
        shell=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "core.autocrlf", "false")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    return path


def write_blob(repo: Path, relative: str, data: bytes) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode("ascii").strip()


def make_index_fixture(root: Path):
    fixture = init_repo(root / "fixture")
    write_blob(fixture, "src/sample.py", SAMPLE_PY)
    write_blob(fixture, "docs/note.md", b"# Note\n")
    write_blob(fixture, "data/config.json", b'{"ok": true}\n')
    write_blob(fixture, "bin/data.bin", b"hello\x00world")
    write_blob(fixture, "text/empty.txt", b"")
    write_blob(fixture, "text/invalid.txt", "caf\xe9".encode("latin-1"))
    write_blob(fixture, "paths/file with space.txt", b"space\n")
    write_blob(fixture, "paths/unicod\u0119.txt", "unicode\n".encode("utf-8"))
    write_blob(fixture, "broken/syntax.py", b"def broken(\n")
    large = b"# large\n" + (b"x = 1\n" * 200_000)
    assert len(large) > 1 * 1024 * 1024
    write_blob(fixture, "src/too_large.py", large)
    write_blob(fixture, "src/side_effect.py", b"raise SystemExit('executed')\n")
    commit1 = commit_all(fixture, "baseline index fixtures")

    link_path = fixture / "links" / "note_link"
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_path.symlink_to("docs/note.md")
        git(fixture, "add", "links/note_link")
        commit2 = commit_all(fixture, "add symlink")
        has_symlink = True
    except (OSError, NotImplementedError):
        write_blob(fixture, "links/note_link.txt", b"fallback\n")
        commit2 = commit_all(fixture, "add link fallback")
        has_symlink = False

    nested = init_repo(root / "nested")
    write_blob(nested, "README.txt", b"nested\n")
    nested_sha = commit_all(nested, "nested baseline")
    git(
        fixture,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{nested_sha},vendor/nested",
    )
    # Do not run `git add -A` here; it can drop a pure cacheinfo gitlink on some platforms.
    git(fixture, "commit", "-m", "add gitlink metadata")
    commit3 = git(fixture, "rev-parse", "HEAD").decode("ascii").strip()

    control = root / "control"
    control.mkdir(parents=True)
    worktrees = root / "worktrees"
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    cfg = BridgeConfig(
        control,
        fixture,
        worktrees,
        repository_id=REPO_ID,
        runtime_dir=runtime,
        journal_path=runtime / "journal.db",
        allowed_paths=("src/sample.py",),
        python_executable=sys.executable,
        test_timeout_seconds=20,
    )
    journal = Journal.open(cfg.journal_path, now_fn=lambda: NOW)
    return cfg, journal, fixture, {
        "commit1": commit1,
        "commit2": commit2,
        "commit3": commit3,
        "nested_sha": nested_sha,
        "has_symlink": has_symlink,
    }


def write_config(path: Path, cfg: BridgeConfig) -> Path:
    target = path / "config.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(cfg.control_repo_path),
                "fixture_repo_path": str(cfg.fixture_repo_path),
                "worktree_root": str(cfg.worktree_root),
                "runtime_dir": str(cfg.runtime_dir),
                "journal_path": str(cfg.journal_path),
                "repository_id": cfg.repository_id,
                "allowed_paths": list(cfg.allowed_paths),
                "commands_ref": cfg.commands_ref,
                "results_ref": cfg.results_ref,
                "python_executable": sys.executable,
                "heartbeat_interval_seconds": 0.05,
                "heartbeat_stale_seconds": 2.0,
                "idle_poll_seconds": 0.05,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return target
