from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from bdb_bridge.windows_stdio import resolve_native_binary_stdio


ROOT = Path(__file__).resolve().parents[1]


class _TextWrapper:
    def __init__(self, buffer: io.BytesIO) -> None:
        self.buffer = buffer


def test_resolve_native_binary_stdio_uses_existing_binary_streams() -> None:
    input_buffer = io.BytesIO(b"input")
    output_buffer = io.BytesIO()

    resolved_input, resolved_output = resolve_native_binary_stdio(
        platform_name="posix",
        stdin=_TextWrapper(input_buffer),
        stdout=_TextWrapper(output_buffer),
    )

    assert resolved_input is input_buffer
    assert resolved_output is output_buffer


def test_resolve_native_binary_stdio_reopens_inherited_windows_handles() -> None:
    opened: list[tuple[int, int]] = []
    streams: dict[int, io.BytesIO] = {
        100: io.BytesIO(b"request"),
        200: io.BytesIO(),
    }

    def get_std_handle(identifier: int) -> int:
        return {-10: 10, -11: 20}[identifier]

    def open_osfhandle(handle: int, flags: int) -> int:
        opened.append((handle, flags))
        return {10: 100, 20: 200}[handle]

    def fdopen(descriptor: int, mode: str, *, buffering: int) -> io.BytesIO:
        assert buffering == 0
        assert mode in {"rb", "wb"}
        return streams[descriptor]

    resolved_input, resolved_output = resolve_native_binary_stdio(
        platform_name="nt",
        stdin=None,
        stdout=None,
        get_std_handle=get_std_handle,
        open_osfhandle=open_osfhandle,
        fdopen=fdopen,
    )

    assert resolved_input is streams[100]
    assert resolved_output is streams[200]
    assert opened == [
        (10, os.O_RDONLY | getattr(os, "O_BINARY", 0)),
        (20, os.O_WRONLY | getattr(os, "O_BINARY", 0)),
    ]


def test_resolve_native_binary_stdio_rejects_missing_non_windows_streams() -> None:
    with pytest.raises(RuntimeError, match="binary stdio is unavailable"):
        resolve_native_binary_stdio(
            platform_name="posix",
            stdin=None,
            stdout=None,
        )


def test_windowless_native_host_entry_uses_inherited_binary_stdio() -> None:
    entry = (ROOT / "packaging" / "windows" / "native_host_entry.py").read_text(
        encoding="utf-8"
    )
    assert "resolve_native_binary_stdio" in entry
    assert "run_project_launcher_host(" in entry
    assert "sys.stdin.buffer" not in entry
    assert "sys.stdout.buffer" not in entry
