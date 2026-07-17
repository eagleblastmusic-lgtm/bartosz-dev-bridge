from __future__ import annotations

import ctypes
import hashlib
import os
import threading
from pathlib import Path


_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000


def wake_event_name(runtime_dir: str | Path) -> str:
    normalized = str(Path(runtime_dir).expanduser().resolve(strict=False)).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"Local\\BDB-{digest}"


class BridgeWakeWaiter:
    """Bridge-compatible waiter backed by a Windows named event when available."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.name = wake_event_name(runtime_dir)
        self._fallback = threading.Event()
        self._handle: int | None = None
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateEventW.restype = ctypes.c_void_p
            handle = kernel32.CreateEventW(None, True, False, self.name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateEventW failed")
            self._handle = int(handle)

    def wait(self, timeout: float | None = None) -> bool:
        if self._handle is None:
            return self._fallback.wait(timeout)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        result = kernel32.WaitForSingleObject(self._handle, milliseconds)
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")

    def clear(self) -> None:
        if self._handle is None:
            self._fallback.clear()
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
        kernel32.ResetEvent.restype = ctypes.c_bool
        if not kernel32.ResetEvent(self._handle):
            raise OSError(ctypes.get_last_error(), "ResetEvent failed")

    def set(self) -> None:
        if self._handle is None:
            self._fallback.set()
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent.restype = ctypes.c_bool
        if not kernel32.SetEvent(self._handle):
            raise OSError(ctypes.get_last_error(), "SetEvent failed")

    def close(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle, self._handle = self._handle, None
        kernel32.CloseHandle(handle)


def signal_running_bridge(runtime_dir: str | Path) -> bool:
    """Signal an existing Windows Bridge event without creating a public endpoint."""

    if os.name != "nt":
        return False
    name = wake_event_name(runtime_dir)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.SetEvent.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE | _SYNCHRONIZE, False, name)
    if not handle:
        return False
    try:
        if not kernel32.SetEvent(handle):
            raise OSError(ctypes.get_last_error(), "SetEvent failed")
        return True
    finally:
        kernel32.CloseHandle(handle)
