from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge.edit_operation_parser import sha256_bytes
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.multi_file_patch_parser import parse_multi_file_patch
from bdb_bridge.multi_file_patch_planner import MultiFilePatchPlanner
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_manager import WorkspaceManager


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b702"
BASE_SHA = "b" * 40


def content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_bytes(content),
    }


def replacement(path: str, before: bytes, after: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-file-replacement-v1",
        "kind": "replace_file",
        "path": path,
        "expected_sha256": sha256_bytes(before),
        **content_fields(after),
    }


def create(path: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "create_file",
        "path": path,
        **content_fields(content),
    }


def delete(path: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "delete_file",
        "path": path,
        "expected_sha256": sha256_bytes(content),
    }


def relocate(kind: str, source: str, destination: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": kind,
        "source_path": source,
        "destination_path": destination,
        "expected_source_sha256": sha256_bytes(content),
    }


def batch(*operations: dict[str, object]) -> dict[str, object]:
    return {"schema": "bdb-multi-file-patch-v1", "operations": list(operations)}


def planner(
    tmp_path: Path,
    *,
    local_paths: tuple[str, ...] = ("*",),
    manifest_paths: list[str] | None = None,
) -> tuple[MultiFilePatchPlanner, Path]:
    config = SimpleNamespace(
        fixture_repo_path=tmp_path / "source",
        worktree_root=tmp_path / "worktrees",
        allowed_paths=local_paths,
    )
    workspace = WorkspaceManager(
        config,
        SESSION_ID,
        BASE_SHA,
        manifest_paths if manifest_paths is not None else ["*"],
    )
    workspace.path.mkdir(parents=True)
    return MultiFilePatchPlanner(workspace), workspace.path


def test_batch_parser_is_canonical_strict_and_bounded() -> None:
    document = batch(
        replacement("a.txt", b"a", b"A"),
        create("new.bin", b"\x00new\xff"),
        delete("old.txt", b"old"),
    )
    first = parse_multi_file_patch(document)
    reordered = {
        "operations": [dict(reversed(list(item.items()))) for item in document["operations"]],
        "schema": "bdb-multi-file-patch-v1",
    }
    second = parse_multi_file_patch(reordered)
    assert first == second
    assert first.operation_count == 3
    assert first.supplied_content_bytes == len(b"A") + len(b"\x00new\xff")
    assert first.patch_sha256.startswith("sha256:")

    invalid_documents = [
        {**document, "extra": True},
        {"schema": "bdb-multi-file-patch-v1", "operations": []},
        batch({"schema": "unknown-v1", "kind": "unknown"}),
        batch({**replacement("a.txt", b"a", b"A"), "content_base64": "QQ==="}),
        batch(replacement(".env.production", b"a", b"A")),
        {"schema": "bdb-multi-file-patch-v1", "operations": [create(f"f{index}.txt", b"") for index in range(101)]},
    ]
    for invalid in invalid_documents:
        with pytest.raises(BridgeError):
            parse_multi_file_patch(invalid)


def test_planner_simulates_mixed_operations_without_mutating_workspace(tmp_path: Path) -> None:
    service, root = planner(tmp_path)
    (root / "same").mkdir()
    (root / "from").mkdir()
    (root / "archive").mkdir()
    (root / "a.txt").write_bytes(b"a")
    (root / "from" / "move.txt").write_bytes(b"move")
    (root / "delete.txt").write_bytes(b"delete")

    patch = parse_multi_file_patch(
        batch(
            replacement("a.txt", b"a", b"A"),
            relocate("rename_file", "a.txt", "renamed.txt", b"A"),
            create("same/new.txt", b"new"),
            replacement("same/new.txt", b"new", b"newer"),
            relocate("move_file", "from/move.txt", "archive/move.txt", b"move"),
            delete("delete.txt", b"delete"),
        )
    )
    first = service.plan(patch)
    second = service.plan(patch)
    assert first == second
    assert first.changed_paths == (
        "a.txt",
        "archive/move.txt",
        "delete.txt",
        "from/move.txt",
        "renamed.txt",
        "same/new.txt",
    )
    by_path = {item.path: item for item in first.paths}
    assert by_path["a.txt"].before == b"a" and by_path["a.txt"].after is None
    assert by_path["renamed.txt"].before is None and by_path["renamed.txt"].after == b"A"
    assert by_path["same/new.txt"].after == b"newer"
    assert by_path["archive/move.txt"].after == b"move"
    assert by_path["delete.txt"].after is None
    assert (root / "a.txt").read_bytes() == b"a"
    assert not (root / "renamed.txt").exists()
    assert not (root / "same" / "new.txt").exists()
    assert (root / "from" / "move.txt").read_bytes() == b"move"
    service.revalidate(first)


def test_planner_rejects_wrong_preconditions_conflicts_and_net_noop(tmp_path: Path) -> None:
    service, root = planner(tmp_path)
    (root / "same").mkdir()
    (root / "source.txt").write_bytes(b"source")
    (root / "same" / "destination.txt").write_bytes(b"destination")

    invalid = [
        batch(replacement("source.txt", b"wrong", b"new")),
        batch(delete("missing.txt", b"missing")),
        batch(relocate("rename_file", "same/destination.txt", "same/existing.txt", b"destination"), create("same/existing.txt", b"later")),
        batch(replacement("source.txt", b"source", b"source")),
    ]
    for document in invalid:
        with pytest.raises(BridgeError):
            service.plan(parse_multi_file_patch(document))

    (root / "same" / "existing.txt").write_bytes(b"existing")
    collision = parse_multi_file_patch(
        batch(relocate("rename_file", "same/destination.txt", "same/existing.txt", b"destination"))
    )
    with pytest.raises(BridgeError) as exists:
        service.plan(collision)
    assert exists.value.code == BridgeErrorCode.STATE_MISMATCH


def test_planner_enforces_local_and_manifest_scope_for_every_role(tmp_path: Path) -> None:
    service, root = planner(
        tmp_path,
        local_paths=("allowed/*",),
        manifest_paths=["allowed/source.txt", "allowed/destination.txt"],
    )
    (root / "allowed").mkdir()
    (root / "other").mkdir()
    (root / "allowed" / "source.txt").write_bytes(b"source")

    allowed = parse_multi_file_patch(
        batch(relocate("rename_file", "allowed/source.txt", "allowed/destination.txt", b"source"))
    )
    assert service.plan(allowed).changed_paths == (
        "allowed/destination.txt",
        "allowed/source.txt",
    )

    manifest_denied = parse_multi_file_patch(create("allowed/other.txt", b"new"))
    with pytest.raises(BridgeError) as manifest:
        service.plan(manifest_denied)
    assert manifest.value.code == BridgeErrorCode.SCOPE_VIOLATION

    local_denied = parse_multi_file_patch(create("other/new.txt", b"new"))
    with pytest.raises(BridgeError) as local:
        service.plan(local_denied)
    assert local.value.code == BridgeErrorCode.POLICY_DENIED


def test_revalidate_detects_workspace_changes_and_plan_tampering(tmp_path: Path) -> None:
    service, root = planner(tmp_path)
    target = root / "target.txt"
    target.write_bytes(b"before")
    plan = service.plan(parse_multi_file_patch(batch(replacement("target.txt", b"before", b"after"))))
    target.write_bytes(b"changed")
    with pytest.raises(BridgeError) as changed:
        service.revalidate(plan)
    assert changed.value.code == BridgeErrorCode.STATE_MISMATCH

    target.write_bytes(b"before")
    tampered = replace(plan, total_after_bytes=plan.total_after_bytes + 1)
    with pytest.raises(BridgeError) as invalid:
        service.revalidate(tampered)
    assert invalid.value.code == BridgeErrorCode.INVALID_PAYLOAD
