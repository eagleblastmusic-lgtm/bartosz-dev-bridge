from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class CompletedCommand:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CompletedCommand:
        ...


class SubprocessCommandRunner:
    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CompletedCommand:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=timeout_seconds,
        )
        return CompletedCommand(
            args=tuple(str(item) for item in args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
