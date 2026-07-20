from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping


_NATIVE_HOST_MANIFEST = Path("BartoszDevBridge") / "com.bartosz.dev_bridge.json"
_PYTHON_NAMES = {
    "python",
    "python.exe",
    "python3",
    "python3.exe",
    "pythonw.exe",
}


def _existing_file(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    try:
        candidate = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _native_host_python(environment: Mapping[str, str]) -> Path | None:
    local_app_data = environment.get("LOCALAPPDATA")
    if not local_app_data:
        return None

    manifest = Path(local_app_data) / _NATIVE_HOST_MANIFEST
    if not manifest.is_file():
        return None

    try:
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        host_path = _existing_file(document.get("path"))
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return None

    if host_path is None:
        return None

    for name in ("python.exe", "python"):
        candidate = _existing_file(host_path.with_name(name))
        if candidate is not None:
            return candidate
    return None


def default_python_executable(
    *,
    current_executable: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    module_file: str | Path | None = None,
) -> str:
    env = os.environ if environment is None else environment

    override = _existing_file(env.get("BDB_PYTHON_EXECUTABLE"))
    if override is not None:
        return str(override)

    current = _existing_file(current_executable or sys.executable)
    if current is not None and current.name.casefold() in _PYTHON_NAMES:
        return str(current)

    source_file = Path(module_file or __file__).resolve(strict=False)
    repo_root = source_file.parents[1]
    for relative in (
        Path(".venv") / "Scripts" / "python.exe",
        Path(".venv") / "bin" / "python",
    ):
        candidate = _existing_file(repo_root / relative)
        if candidate is not None:
            return str(candidate)

    native_python = _native_host_python(env)
    return str(native_python) if native_python is not None else ""
