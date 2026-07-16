from __future__ import annotations

import json
import sqlite3
import subprocess
from functools import wraps
from typing import Any, Callable, Type

from .journal import Journal
from .manual_preserve import install_manual_pre_preserve
from .migrations import map_sqlite_error
from .protocol import BridgeError, sanitize_diagnostics

_PUBLIC_BOUNDARY_ERRORS = (
    UnicodeError,
    json.JSONDecodeError,
    subprocess.TimeoutExpired,
    subprocess.CalledProcessError,
    FileNotFoundError,
    OSError,
    ValueError,
)


def install_workspace_lifecycle_error_mapping(coordinator_cls: Type[object]) -> None:
    for name in ("assess_cleanup", "preserve", "status", "cleanup"):
        original = getattr(coordinator_cls, name)
        if getattr(original, "_ghb07_error_mapped", False):
            continue

        @wraps(original)
        def wrapped(self: object, *args: Any, __original: Callable[..., Any] = original, **kwargs: Any):
            try:
                return __original(self, *args, **kwargs)
            except BridgeError:
                raise
            except sqlite3.Error as exc:
                raise map_sqlite_error(exc, context="workspace lifecycle") from exc
            except _PUBLIC_BOUNDARY_ERRORS as exc:
                detail = sanitize_diagnostics(
                    f"workspace lifecycle boundary failure: {type(exc).__name__}",
                    limit=500,
                )
                raise BridgeError("unsafe_worktree_path", detail) from exc

        wrapped._ghb07_error_mapped = True
        setattr(coordinator_cls, name, wrapped)


install_manual_pre_preserve(Journal)
