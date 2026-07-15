from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bdb_bridge import BridgeError, BridgeErrorCode, CommandSnapshot, RemoteDocument
from bdb_poc.git_ops import ControlRepository
from bdb_poc.transport import GitCommandTransport
from tests.helpers.git_control_repo import (
    BASE_SHA,
    SESSION_ID,
    command_payload,
    commit_and_push_commands,
    fetch_clone,
    init_control_remote,
    manifest_payload,
    write_command,
    write_manifest,
)


def test_snapshot_uses_single_sha(tmp_path: Path) -> None:
    fixture = init_control_remote(tmp_path)
    write_manifest(fixture.writer, SESSION_ID, manifest_payload())
    write_command(fixture.writer, SESSION_ID, 1, command_payload(sequence=1))
    sha = commit_and_push_commands(fixture.writer)
    fetch_clone(fixture.clone)

    transport = GitCommandTransport(fixture.clone)
    snapshot = transport.fetch_snapshot()
    assert snapshot.snapshot_sha == sha
    assert len(snapshot.manifests) == 1
    assert len(snapshot.commands) == 1


def test_branch_move_without_fetch_keeps_snapshot_sha(tmp_path: Path) -> None:
    fixture = init_control_remote(tmp_path)
    write_manifest(fixture.writer, SESSION_ID, manifest_payload())
    write_command(fixture.writer, SESSION_ID, 1, command_payload(sequence=1))
    commit_and_push_commands(fixture.writer)
    fetch_clone(fixture.clone)

    transport = GitCommandTransport(fixture.clone)
    original = transport.fetch_snapshot()

    write_command(fixture.writer, SESSION_ID, 2, command_payload(sequence=2))
    commit_and_push_commands(fixture.writer, "add command 2")

    reread = transport.fetch_snapshot()
    assert reread.snapshot_sha == original.snapshot_sha
    assert len(reread.commands) == 1


def test_document_commit_sha_is_per_document(tmp_path: Path) -> None:
    fixture = init_control_remote(tmp_path)
    write_manifest(fixture.writer, SESSION_ID, manifest_payload())
    write_command(fixture.writer, SESSION_ID, 1, command_payload(sequence=1))
    commit_and_push_commands(fixture.writer)
    fetch_clone(fixture.clone)

    transport = GitCommandTransport(fixture.clone)
    first = transport.fetch_snapshot()
    cmd1_sha = first.commands[0].document_commit_sha

    write_command(fixture.writer, SESSION_ID, 2, command_payload(sequence=2))
    commit_and_push_commands(fixture.writer, "add command 2")
    fetch_clone(fixture.clone)

    second = GitCommandTransport(fixture.clone).fetch_snapshot()
    old_cmd = next(item for item in second.commands if item.path.endswith("000001.json"))
    new_cmd = next(item for item in second.commands if item.path.endswith("000002.json"))
    assert second.snapshot_sha != first.snapshot_sha
    assert old_cmd.document_commit_sha == cmd1_sha
    assert new_cmd.document_commit_sha != cmd1_sha


def test_invalid_path_not_in_snapshot(tmp_path: Path) -> None:
    fixture = init_control_remote(tmp_path)
    write_manifest(fixture.writer, SESSION_ID, manifest_payload())
    arbitrary = fixture.writer / "sessions" / SESSION_ID / "payloads" / "x.json"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("{}", encoding="utf-8")
    commit_and_push_commands(fixture.writer)
    fetch_clone(fixture.clone)

    snapshot = GitCommandTransport(fixture.clone).fetch_snapshot()
    paths = {item.path for item in snapshot.manifests + snapshot.commands}
    assert "sessions/" not in {p.split("/")[0] + "/" for p in paths if "payloads" in p}
    assert all("commands" in p or p.endswith("manifest.json") for p in paths)


def test_transient_fetch_error_mapped(tmp_path: Path) -> None:
    transport = GitCommandTransport(tmp_path / "missing")
    with pytest.raises(BridgeError) as exc:
        transport.fetch_snapshot()
    assert exc.value.code == BridgeErrorCode.TRANSPORT_UNAVAILABLE.value


def test_control_repository_compatibility(tmp_path: Path) -> None:
    fixture = init_control_remote(tmp_path)
    write_manifest(fixture.writer, SESSION_ID, manifest_payload())
    write_command(fixture.writer, SESSION_ID, 1, command_payload(sequence=1))
    commit_and_push_commands(fixture.writer)
    fetch_clone(fixture.clone)

    control = ControlRepository(fixture.clone)
    control.fetch()
    paths = control.list_command_paths()
    assert any(path.endswith("000001.json") for path in paths)
    manifest = control.read_json("origin/commands", f"sessions/{SESSION_ID}/manifest.json")
    assert manifest["session_id"] == SESSION_ID


class FakeTransport:
    def __init__(self, snapshots: list[CommandSnapshot], *, fail_times: int = 0) -> None:
        self._snapshots = list(snapshots)
        self._fail_times = fail_times
        self.calls = 0

    def fetch_snapshot(self) -> CommandSnapshot:
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "network down")
        if not self._snapshots:
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "empty")
        return self._snapshots.pop(0)


def test_immutable_snapshot_class(tmp_path: Path) -> None:
    doc = RemoteDocument(path="sessions/x/manifest.json", content="{}", document_commit_sha="b" * 40)
    snap = CommandSnapshot(snapshot_sha="a" * 40, manifests=(doc,), commands=())
    assert snap.snapshot_sha == "a" * 40
