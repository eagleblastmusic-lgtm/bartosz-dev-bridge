from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Protocol, Sequence


WINDOWS_CREATE_NO_WINDOW = 0x08000000


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
    def __init__(self, *, platform_name: str | None = None) -> None:
        self._platform_name = platform_name or os.name

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CompletedCommand:
        # Capture through regular temporary files instead of PIPE. Long-lived
        # descendants may inherit standard handles on Windows; PIPE capture can
        # then keep subprocess.run()/communicate() blocked after the operator
        # process itself has already exited.
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            run_options: dict[str, object] = {
                "stdin": subprocess.DEVNULL,
                "stdout": stdout_file,
                "stderr": stderr_file,
                "check": False,
                "shell": False,
                "timeout": timeout_seconds,
            }
            if self._platform_name == "nt":
                # Control Center is a GUI process. Without CREATE_NO_WINDOW,
                # synchronous PowerShell/Python helpers can create a visible
                # console window every time status, Start, Stop or Re-arm runs.
                run_options["creationflags"] = WINDOWS_CREATE_NO_WINDOW

            completed = subprocess.run(list(args), **run_options)
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
