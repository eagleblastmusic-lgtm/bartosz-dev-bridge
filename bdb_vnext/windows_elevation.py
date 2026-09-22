"""Windows UAC elevation helper for canonical BDB maintenance.

This module is deliberately tiny and separate from Bootstrap authority logic.
It never mutates BDB state itself. It only re-launches an exact Python module
under the Windows runas verb and waits for its process exit code.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Sequence


class ElevationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_elevated() -> bool:
    """Return whether the current Windows process has an administrator token."""

    if os.name != "nt":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run_elevated_python_module(
    module: str,
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
) -> int:
    """Run python -m <module> through UAC and wait for completion."""

    if os.name != "nt":
        raise ElevationError("elevation_unsupported", "UAC elevation is available only on Windows")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    INFINITE = 0xFFFFFFFF
    ERROR_CANCELLED = 1223

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    parameters = subprocess.list2cmdline(["-m", module, *[str(item) for item in args]])
    working_directory = str(Path(cwd).resolve()) if cwd is not None else None
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = parameters
    info.lpDirectory = working_directory
    info.nShow = SW_HIDE

    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise ElevationError("elevation_cancelled", "Windows UAC elevation was cancelled")
        raise ElevationError("elevation_failed", f"Windows UAC elevation failed with error {error}")

    if not info.hProcess:
        raise ElevationError("elevation_process_missing", "Windows did not return an elevated process handle")

    try:
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        wait_result = kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        if wait_result != 0:
            raise ElevationError("elevation_wait_failed", f"waiting for elevated maintenance failed with code {wait_result}")

        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            error = ctypes.get_last_error()
            raise ElevationError("elevation_exit_code_failed", f"cannot read elevated process exit code ({error})")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(info.hProcess)


__all__ = ["ElevationError", "is_elevated", "run_elevated_python_module"]
