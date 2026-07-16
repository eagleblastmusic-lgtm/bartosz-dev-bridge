from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Type

from .multi_file_patch_recovery_models import (
    MultiFileCheckpointBundle,
    MultiFileCheckpointPath,
)


def _command_scoped_temp_path(
    self: Any,
    bundle: MultiFileCheckpointBundle,
    item: MultiFileCheckpointPath,
    mode: str,
) -> Path:
    del bundle
    target = self.workspace.resolve_allowed_path(item.path)
    command_digest = hashlib.sha256(item.command_id.encode("utf-8")).hexdigest()[:16]
    path_digest = hashlib.sha256(item.path.encode("utf-8")).hexdigest()[:16]
    return target.parent / (
        f".bdb_batch_{command_digest}_{path_digest}_{item.ordinal}_{mode}"
    )


def install_multi_file_patch_temp_identity(executor_cls: Type[object]) -> None:
    """Use a bounded temp identity independent of mutable workspace contents."""

    if getattr(executor_cls, "_ghb2c_temp_identity_installed", False):
        return
    setattr(executor_cls, "_temp_path", _command_scoped_temp_path)
    setattr(executor_cls, "_ghb2c_temp_identity_installed", True)
