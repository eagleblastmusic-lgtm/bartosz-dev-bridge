from __future__ import annotations

import os
import sys
from typing import BinaryIO, Callable


_MISSING = object()
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11


def _binary_buffer(stream: object) -> BinaryIO | None:
    candidate = getattr(stream, "buffer", None)
    return candidate if candidate is not None else None


def _windows_std_handle(identifier: int) -> int:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    value = kernel32.GetStdHandle(identifier & 0xFFFFFFFF)
    handle = 0 if value is None else int(value)
    invalid_handle = int(ctypes.c_void_p(-1).value)
    if handle in (0, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, f"GetStdHandle({identifier}) returned no inherited pipe")
    return handle


def _windows_binary_stream(
    handle: int,
    *,
    mode: str,
    flags: int,
    open_osfhandle: Callable[[int, int], int] | None = None,
    fdopen: Callable[..., BinaryIO] = os.fdopen,
) -> BinaryIO:
    if open_osfhandle is None:
        import msvcrt

        open_osfhandle = msvcrt.open_osfhandle
    descriptor = open_osfhandle(handle, flags)
    try:
        return fdopen(descriptor, mode, buffering=0)
    except Exception:
        os.close(descriptor)
        raise


def resolve_native_binary_stdio(
    *,
    platform_name: str | None = None,
    stdin: object = _MISSING,
    stdout: object = _MISSING,
    get_std_handle: Callable[[int], int] | None = None,
    open_osfhandle: Callable[[int, int], int] | None = None,
    fdopen: Callable[..., BinaryIO] = os.fdopen,
) -> tuple[BinaryIO, BinaryIO]:
    """Return binary Native Messaging streams for console and GUI executables.

    PyInstaller's Windows GUI bootloader intentionally sets ``sys.stdin`` and
    ``sys.stdout`` to ``None``. Chrome still supplies inherited anonymous-pipe
    handles for a Native Messaging host, so a windowless executable can reopen
    those exact handles without allocating a console or changing the protocol.
    """

    actual_platform = platform_name or os.name
    actual_stdin = sys.stdin if stdin is _MISSING else stdin
    actual_stdout = sys.stdout if stdout is _MISSING else stdout
    input_stream = _binary_buffer(actual_stdin)
    output_stream = _binary_buffer(actual_stdout)
    if input_stream is not None and output_stream is not None:
        return input_stream, output_stream
    if actual_platform != "nt":
        raise RuntimeError("Native Messaging binary stdio is unavailable")

    handle_reader = get_std_handle or _windows_std_handle
    binary_flag = getattr(os, "O_BINARY", 0)
    if input_stream is None:
        input_stream = _windows_binary_stream(
            handle_reader(_STD_INPUT_HANDLE),
            mode="rb",
            flags=os.O_RDONLY | binary_flag,
            open_osfhandle=open_osfhandle,
            fdopen=fdopen,
        )
    if output_stream is None:
        output_stream = _windows_binary_stream(
            handle_reader(_STD_OUTPUT_HANDLE),
            mode="wb",
            flags=os.O_WRONLY | binary_flag,
            open_osfhandle=open_osfhandle,
            fdopen=fdopen,
        )
    return input_stream, output_stream
