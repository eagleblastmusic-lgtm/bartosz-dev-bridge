from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge.edit_operation_models import MAX_STRUCTURAL_CONTENT_BYTES, StructuralEditKind
from bdb_bridge.edit_operation_parser import parse_structural_edit, sha256_bytes
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.protocol import BridgeError
from bdb_bridge.structural_edit_engine import StructuralEditEngine
from bdb_bridge.workspace_manager import WorkspaceManager


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
BASE_SHA = "a" * 40


def encoded(value: bytes) -> tuple[str, str]:
    return base64.b64encode(value).decode("ascii"), sha256_bytes(value)


def create_document(path: str, content: bytes) -> dict[str, str]:
    body, digest = encoded(content)
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "create_file",
        "path": path,
        "content_base64": body,
        "content_sha256": digest,
    }


def delete_document(path: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": "delete_file",
        "path": path,
        "expected_sha256": sha256_bytes(content),
    }


def relocate_document(kind: str, source: str, destination: str, content: bytes) -> dict[str, str]:
    return {
        "schema": "bdb-edit-operation-v1",
        "kind": kind,
        "source_path": source,
        "destination_path": destination,
        "expected_source_sha256": sha256_bytes(content),
    }


def engine(tmp_path: Path) -> tuple[StructuralEditEngine, Path]:
    config = SimpleNamespace(
        fixture_repo_path=tmp_path / "source",
        worktree_root=tmp_path / "worktrees",
        allowed_paths=("*",),
    )
    workspace = WorkspaceManager(config, SESSION_ID, BASE_SHA, ["*"])
    workspace.path.mkdir(parents=True)
    return StructuralEditEngine(workspace), workspace.path


@pytest.mark.parametrize(
    ("document", "kind", "source", "destination"),
    [
        (create_document("new.bin", b"\x00new\xff"), StructuralEditKind.CREATE_FILE, None, "new.bin"),
        (delete_document("old.bin", b"old"), StructuralEditKind.DELETE_FILE, "old.bin", None),
        (relocate_document("rename_file", "same/a.bin", "same/b.bin", b"a"), StructuralEditKind.RENAME_FILE, "same/a.bin", "same/b.bin"),
        (relocate_document("move_file", "from/a.bin", "to/a.bin", b"a"), StructuralEditKind.MOVE_FILE, "from/a.bin", "to/a.bin"),
    ],
)
def test_structural_parser_is_canonical_and_deterministic(document, kind, source, destination) -> None:
    first = parse_structural_edit(document)
    second = parse_structural_edit(dict(reversed(list(document.items()))))
    assert first == second
    assert first.kind is kind
    assert first.source_path == source
    assert first.destination_path == destination
    assert first.operation_sha256.startswith("sha256:")


@pytest.mark.parametrize(
    "document",
    [
        {**create_document("new.bin", b"new"), "extra": True},
        {**create_document("new.bin", b"new"), "content_base64": "bmV3=="},
        {**create_document("new.bin", b"new"), "content_sha256": "sha256:" + "0" * 64},
        delete_document("../escape.bin", b"old"),
        delete_document(".env.production", b"secret"),
        relocate_document("rename_file", "a/x.bin", "b/y.bin", b"x"),
        relocate_document("move_file", "a/x.bin", "a/y.bin", b"x"),
    ],
)
def test_structural_parser_rejects_noncanonical_or_unsafe_documents(document) -> None:
    with pytest.raises(BridgeError):
        parse_structural_edit(document)


@pytest.mark.parametrize("kind", ["create_file", "delete_file", "rename_file", "move_file"])
def test_structural_engine_applies_exactly_one_operation(tmp_path: Path, kind: str) -> None:
    edit, root = engine(tmp_path)
    content = b"binary\x00payload\xff"
    if kind == "create_file":
        document = create_document("new.bin", content)
        source = None
        destination = root / "new.bin"
    elif kind == "delete_file":
        source = root / "delete.bin"
        source.write_bytes(content)
        destination = None
        document = delete_document("delete.bin", content)
    elif kind == "rename_file":
        (root / "same").mkdir()
        source = root / "same" / "old.bin"
        source.write_bytes(content)
        destination = root / "same" / "new.bin"
        document = relocate_document("rename_file", "same/old.bin", "same/new.bin", content)
    else:
        (root / "from").mkdir()
        (root / "to").mkdir()
        source = root / "from" / "old.bin"
        source.write_bytes(content)
        destination = root / "to" / "new.bin"
        document = relocate_document("move_file", "from/old.bin", "to/new.bin", content)

    operation = parse_structural_edit(document)
    first_plan = edit.plan(operation)
    second_plan = edit.plan(operation)
    assert first_plan == second_plan
    outcome = edit.apply(first_plan)
    assert outcome.kind.value == kind
    assert outcome.outcome_sha256.startswith("sha256:")
    assert outcome.plan_sha256 == first_plan.plan_sha256
    if source is not None:
        assert source.exists() is False
    if destination is not None:
        assert destination.read_bytes() == content
        assert outcome.destination_sha256_after == sha256_bytes(content)
    assert not list(root.rglob(".bdb_edit_*"))


def test_engine_revalidates_source_and_never_overwrites_destination(tmp_path: Path) -> None:
    edit, root = engine(tmp_path)
    source = root / "old.bin"
    source.write_bytes(b"before")
    delete_plan = edit.plan(parse_structural_edit(delete_document("old.bin", b"before")))
    source.write_bytes(b"changed")
    with pytest.raises(BridgeError) as changed:
        edit.apply(delete_plan)
    assert changed.value.code == BridgeErrorCode.STATE_MISMATCH
    assert source.read_bytes() == b"changed"

    create_plan = edit.plan(parse_structural_edit(create_document("new.bin", b"planned")))
    (root / "new.bin").write_bytes(b"foreign")
    with pytest.raises(BridgeError) as exists:
        edit.apply(create_plan)
    assert exists.value.code == BridgeErrorCode.STATE_MISMATCH
    assert (root / "new.bin").read_bytes() == b"foreign"


def test_engine_rejects_missing_parent_large_source_and_tampered_plan(tmp_path: Path) -> None:
    edit, root = engine(tmp_path)
    with pytest.raises(BridgeError) as parent:
        edit.plan(parse_structural_edit(create_document("missing/new.bin", b"new")))
    assert parent.value.code == BridgeErrorCode.MISSING_FILE

    large = b"x" * (MAX_STRUCTURAL_CONTENT_BYTES + 1)
    (root / "large.bin").write_bytes(large)
    with pytest.raises(BridgeError) as bounded:
        edit.plan(parse_structural_edit(delete_document("large.bin", large)))
    assert bounded.value.code == BridgeErrorCode.POLICY_DENIED

    operation = parse_structural_edit(create_document("safe.bin", b"safe"))
    plan = edit.plan(operation)
    tampered = replace(plan, destination_after=b"different")
    with pytest.raises(BridgeError) as mismatch:
        edit.apply(tampered)
    assert mismatch.value.code == BridgeErrorCode.INVALID_PAYLOAD
    assert not (root / "safe.bin").exists()

    invalid_operation = replace(operation, operation_sha256="sha256:" + "0" * 64)
    with pytest.raises(BridgeError) as invalid:
        edit.plan(invalid_operation)
    assert invalid.value.code == BridgeErrorCode.INVALID_PAYLOAD


def test_engine_rejects_existing_relocation_destination_and_temp_collision(tmp_path: Path) -> None:
    edit, root = engine(tmp_path)
    (root / "same").mkdir()
    source = root / "same" / "old.bin"
    destination = root / "same" / "new.bin"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    operation = parse_structural_edit(
        relocate_document("rename_file", "same/old.bin", "same/new.bin", b"source")
    )
    with pytest.raises(BridgeError) as exists:
        edit.plan(operation)
    assert exists.value.code == BridgeErrorCode.STATE_MISMATCH
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"

    destination.unlink()
    create = parse_structural_edit(create_document("same/new.bin", b"new"))
    plan = edit.plan(create)
    suffix = plan.plan_sha256.removeprefix("sha256:")[:16]
    temp = root / "same" / f".bdb_edit_new.bin_{suffix}"
    temp.write_bytes(b"foreign")
    with pytest.raises(BridgeError) as collision:
        edit.apply(plan)
    assert collision.value.code == BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED
    assert temp.read_bytes() == b"foreign"
    assert not destination.exists()
