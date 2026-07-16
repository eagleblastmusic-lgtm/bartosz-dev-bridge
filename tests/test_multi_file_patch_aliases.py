from __future__ import annotations

import base64
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge.edit_operation_parser import sha256_bytes
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.multi_file_patch_parser import parse_multi_file_patch
from bdb_bridge.multi_file_patch_planner import MultiFilePatchPlanner
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_manager import WorkspaceManager


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b703"
BASE_SHA = "c" * 40


def create(path: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "create_file",
        "path": path,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_bytes(content),
    }


def planner(tmp_path: Path) -> MultiFilePatchPlanner:
    config = SimpleNamespace(
        fixture_repo_path=tmp_path / "source",
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("*",),
    )
    workspace = WorkspaceManager(config, SESSION_ID, BASE_SHA, ["*"])
    workspace.path.mkdir(parents=True)
    return MultiFilePatchPlanner(workspace)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Case.txt", "case.txt"),
        ("café.txt", unicodedata.normalize("NFD", "café.txt")),
    ],
)
def test_planner_rejects_case_and_unicode_path_aliases(
    tmp_path: Path, first: str, second: str
) -> None:
    patch = parse_multi_file_patch(
        {
            "schema": "bdb-multi-file-patch-v1",
            "operations": [create(first, b"first"), create(second, b"second")],
        }
    )
    with pytest.raises(BridgeError) as alias:
        planner(tmp_path).plan(patch)
    assert alias.value.code == BridgeErrorCode.INVALID_PAYLOAD
