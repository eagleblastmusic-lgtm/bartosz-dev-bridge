from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .common import (
    BridgeError,
    COMMAND_PATH_RE,
    MAX_RESULT_BYTES,
    validate_repo_relative_path,
)


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(
        self,
        args: Iterable[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", "-C", str(cwd or self.repo), *list(args)]
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise BridgeError("git_error", f"git {' '.join(args)} failed: {detail}")
        return completed

    def run_bytes(
        self,
        args: Iterable[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        from bdb_bridge.models import BridgeErrorCode
        command = ["git", "-C", str(cwd or self.repo), *list(args)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git {' '.join(args)} timed out: {exc}",
            ) from exc
        except FileNotFoundError as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git executable not found: {exc}",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git execution failed: {exc}",
            ) from exc

        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"git {' '.join(args)} failed with exit code {completed.returncode}: {detail}",
            )
        return completed


class ControlRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git = Git(path)

    def preflight(self) -> None:
        if not (self.path / ".git").exists():
            raise BridgeError("invalid_control_repo", f"Not a Git repository: {self.path}")
        self.git.run(["remote", "get-url", "origin"])

    def fetch(self) -> None:
        self.git.run(
            [
                "fetch",
                "--prune",
                "origin",
                "+refs/heads/commands:refs/remotes/origin/commands",
                "+refs/heads/results:refs/remotes/origin/results",
            ],
            timeout=90,
        )

    def ref_sha(self, ref: str) -> str:
        return self.git.run(["rev-parse", ref]).stdout.strip()

    def list_command_paths(self) -> list[str]:
        completed = self.git.run(
            ["ls-tree", "-r", "--name-only", "origin/commands", "--", "sessions"],
            check=False,
        )
        if completed.returncode not in (0, 128):
            raise BridgeError("git_error", completed.stderr.strip())
        return sorted(path for path in completed.stdout.splitlines() if COMMAND_PATH_RE.fullmatch(path))

    def read_text(self, ref: str, path: str) -> str:
        validate_repo_relative_path(path)
        completed = self.git.run(["show", f"{ref}:{path}"], check=False)
        if completed.returncode != 0:
            raise BridgeError("missing_protocol_file", f"Missing {path} on {ref}")
        return completed.stdout

    def read_json(self, ref: str, path: str) -> dict[str, Any]:
        try:
            value = json.loads(self.read_text(ref, path))
        except json.JSONDecodeError as exc:
            raise BridgeError("invalid_json", f"Invalid JSON at {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise BridgeError("invalid_json", f"JSON object required at {path}")
        return value

    def result_exists(self, result_path: str) -> bool:
        validate_repo_relative_path(result_path)
        return self.git.run(
            ["cat-file", "-e", f"origin/results:{result_path}"], check=False
        ).returncode == 0

    def publish_result(self, result_path: str, serialized: str) -> str:
        validate_repo_relative_path(result_path)
        if len(serialized.encode("utf-8")) > MAX_RESULT_BYTES:
            raise BridgeError("result_too_large", "result.json exceeds 16 KiB")
        last_error = ""
        for attempt in range(1, 4):
            self.fetch()
            if self.result_exists(result_path):
                return self.ref_sha("origin/results")
            branch = f"bdb-poc-results-{os.getpid()}-{attempt}"
            worktree = Path(tempfile.mkdtemp(prefix="bdb-results-"))
            try:
                self.git.run(["branch", "-f", branch, "origin/results"])
                self.git.run(["worktree", "add", "--force", str(worktree), branch])
                destination = worktree / PurePosixPath(result_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(serialized + "\n", encoding="utf-8", newline="\n")
                self.git.run(["add", "--", result_path], cwd=worktree)
                self.git.run(
                    [
                        "-c", "user.name=Bartosz Dev Bridge POC",
                        "-c", "user.email=bdb-poc@users.noreply.github.com",
                        "commit", "-m",
                        f"result: {PurePosixPath(result_path).parent.parent.name} {PurePosixPath(result_path).stem}",
                    ],
                    cwd=worktree,
                )
                push = self.git.run(
                    ["push", "origin", "HEAD:results"], cwd=worktree, check=False, timeout=90
                )
                if push.returncode == 0:
                    return self.git.run(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
                last_error = (push.stderr or push.stdout).strip()
            finally:
                self.git.run(["worktree", "remove", "--force", str(worktree)], check=False)
                self.git.run(["branch", "-D", branch], check=False)
                shutil.rmtree(worktree, ignore_errors=True)
            time.sleep(min(attempt, 2))
        raise BridgeError("result_publication_failed", last_error or "Unable to push result")
