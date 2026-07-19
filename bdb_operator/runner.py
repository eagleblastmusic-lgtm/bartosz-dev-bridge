from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Protocol, Sequence


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
        # Capture through regular temporary files instead of PIPE. Long-lived
        # descendants may inherit standard handles on Windows; PIPE capture can
        # then keep subprocess.run()/communicate() blocked after the operator
        # process itself has already exited.
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            completed = subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                shell=False,
                timeout=timeout_seconds,
            )
            stdout = _read_utf8(stdout_file)
            stderr = _read_utf8(stderr_file)
        return CompletedCommand(
            args=tuple(str(item) for item in args),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )


def _read_utf8(stream: BinaryIO) -> str:
    stream.seek(0)
    return stream.read().decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
