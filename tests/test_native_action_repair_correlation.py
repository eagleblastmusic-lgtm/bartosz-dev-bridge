from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_bridge.native_actions import (
    NativeActionComposer,
    NativeSessionStore,
    RepositoryAlias,
)
from bdb_bridge.protocol import BridgeError
from bdb_bridge.repair_correlation import REPAIR_CORRELATION_SCHEMA

from test_native_action_state_context import (
    fixed_now,
    initialize_repo,
    run,
    write_bridge_config,
)


SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
PREDECESSOR = "018f3f66-6cb3-4f66-9f2e-3d7647d1b700"
CORRELATION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b799"


def writer(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def composer(tmp_path: Path) -> tuple[NativeActionComposer, Path]:
    repo = tmp_path / "repo"
    initialize_repo(repo)
    config_path = tmp_path / "bridge-config.json"
    write_bridge_config(config_path, repo)
    store_path = tmp_path / "native-sessions.json"
    return (
        NativeActionComposer(
            {"sample": RepositoryAlias.load("sample", config_path)},
            NativeSessionStore(store_path, writer=writer),
            now_fn=fixed_now,
        ),
        store_path,
    )


def action(*, sequence: int, repair_correlation: object = None, include: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "bdb-action-v1",
        "repo_alias": "sample",
        "session_id": SESSION_ID,
        "sequence": sequence,
        "operation": "open_read",
        "expected_revision": 0,
        "payload": {"path": "README.md"},
    }
    if include:
        value["repair_correlation"] = repair_correlation
    return value


def explicit_repair(correlation_id: str = CORRELATION_ID) -> dict[str, object]:
    return {
        "schema": REPAIR_CORRELATION_SCHEMA,
        "correlation_id": correlation_id,
        "role": "repair",
        "predecessor_session_id": PREDECESSOR,
    }


def test_native_action_persists_and_reuses_explicit_repair_correlation(tmp_path: Path) -> None:
    native, store_path = composer(tmp_path)

    _, first = native.compose(action(sequence=1, repair_correlation=explicit_repair()))
    _, second = native.compose(action(sequence=2, include=False))

    assert first["manifest"]["repair_correlation"] == explicit_repair()
    assert second["manifest"]["repair_correlation"] == explicit_repair()
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored["sessions"][SESSION_ID]["repair_correlation"] == explicit_repair()


def test_native_action_rejects_correlation_change_within_session(tmp_path: Path) -> None:
    native, _ = composer(tmp_path)
    native.compose(action(sequence=1, repair_correlation=explicit_repair()))

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                sequence=2,
                repair_correlation=explicit_repair(
                    "018f3f66-6cb3-4f66-9f2e-3d7647d1b798"
                ),
            )
        )
    assert error.value.code == "journal_conflict"


def test_uncorrelated_native_session_remains_backward_compatible(tmp_path: Path) -> None:
    native, store_path = composer(tmp_path)

    _, envelope = native.compose(action(sequence=1, include=False))

    assert "repair_correlation" not in envelope["manifest"]
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert "repair_correlation" not in stored["sessions"][SESSION_ID]


def test_dirty_repo_rejects_correlated_session_before_store_write(tmp_path: Path) -> None:
    native, store_path = composer(tmp_path)
    repo = tmp_path / "repo"
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    assert run(repo, "status", "--porcelain=v1").stdout.strip()

    with pytest.raises(BridgeError) as error:
        native.compose(action(sequence=1, repair_correlation=explicit_repair()))
    assert error.value.code == "dirty_source_checkout"
    assert not store_path.exists()
