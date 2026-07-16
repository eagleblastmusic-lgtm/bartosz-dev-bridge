from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Type

from .multi_file_patch_recovery_models import (
    MultiFileCheckpointBundle,
    MultiFileCheckpointPath,
)
from .protocol import validate_repo_relative_path
from .workspace_manager import WorkspaceManager


def _compute_state_hash_with_overrides(
    self: WorkspaceManager,
    overrides: Mapping[str, bytes | None],
) -> str:
    normalized_overrides: dict[str, bytes | None] = {}
    for relative, content in overrides.items():
        normalized = validate_repo_relative_path(relative)
        self.resolve_allowed_path(normalized)
        normalized_overrides[normalized] = content

    head = self.git.run(["rev-parse", "HEAD"]).stdout.strip()
    paths = set(
        self.git.run(["ls-files", "-m", "-o", "--exclude-standard"])
        .stdout.splitlines()
    )
    paths.update(normalized_overrides)
    digest = hashlib.sha256()
    digest.update(b"bdb-poc-state-v1\0")
    digest.update(head.encode("ascii"))
    digest.update(b"\0")
    for relative in sorted(paths):
        if not self.is_allowed_path(relative):
            continue
        normalized = validate_repo_relative_path(relative)
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
        if normalized in normalized_overrides:
            content = normalized_overrides[normalized]
            digest.update(
                hashlib.sha256(content).digest()
                if content is not None
                else b"<missing>"
            )
        else:
            file_path = self.resolve_allowed_path(normalized)
            digest.update(
                hashlib.sha256(file_path.read_bytes()).digest()
                if file_path.is_file()
                else b"<missing>"
            )
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _predicted_state_hash(
    self: Any,
    paths: tuple[MultiFileCheckpointPath, ...],
    *,
    after: bool,
) -> str:
    overrides = {
        item.path: item.after if after else item.before
        for item in paths
    }
    return self.workspace.compute_state_hash_with_overrides(overrides)


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
    """Install stable temp identity and one canonical multi-path state-hash helper."""

    if getattr(executor_cls, "_ghb2c_temp_identity_installed", False):
        return
    setattr(
        WorkspaceManager,
        "compute_state_hash_with_overrides",
        _compute_state_hash_with_overrides,
    )
    setattr(executor_cls, "_predicted_state_hash", _predicted_state_hash)
    setattr(executor_cls, "_temp_path", _command_scoped_temp_path)
    setattr(executor_cls, "_ghb2c_temp_identity_installed", True)
