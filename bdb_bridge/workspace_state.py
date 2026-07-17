from __future__ import annotations

import hashlib

from .protocol import validate_base_sha


def clean_workspace_state_hash(base_sha: str) -> str:
    """Return the canonical state hash for a clean detached worktree at ``base_sha``.

    This is the zero-change case of ``WorkspaceManager.compute_state_hash``. Keeping
    it in one small production helper lets Native Messaging expose the exact initial
    CAS value without creating a worktree or weakening the normal pre-plan gate.
    """

    canonical = validate_base_sha(base_sha)
    digest = hashlib.sha256()
    digest.update(b"bdb-poc-state-v1\0")
    digest.update(canonical.encode("ascii"))
    digest.update(b"\0")
    return "sha256:" + digest.hexdigest()
