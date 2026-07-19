from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bdb_bridge.native_actions import (
    NativeActionComposer,
    NativeSessionStore,
    RepositoryAlias,
)
from bdb_bridge.protocol import BridgeError
from bdb_bridge.repair_correlation import REPAIR_CORRELATION_SCHEMA


INITIAL_SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b700"
REPAIR_SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
SECOND_INITIAL_SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b702"
CORRELATION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b799"
OTHER_CORRELATION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b798"
NOW = datetime(2026, 7, 19, 18, 0, 0, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def initialize_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init")
    git(repo, "config", "user.name", "Repair Correlation Test")
    git(repo, "config", "user.email", "repair-correlation@example.invalid")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(repo, "add", "--", "README.md")
    git(repo, "commit", "-m", "initialize fixture")


def writer(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def configured_alias(
    tmp_path: Path,
    *,
    alias: str,
    repository_id: str,
) -> tuple[RepositoryAlias, Path]:
    repo = tmp_path / f"{alias}-repo"
    control = tmp_path / f"{alias}-control"
    worktrees = tmp_path / f"{alias}-worktrees"
    runtime = tmp_path / f"{alias}-runtime"
    initialize_repo(repo)
    for path in (control, worktrees, runtime):
        path.mkdir(parents=True)
    config_path = tmp_path / f"{alias}-bridge-config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(repo),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "repository_id": repository_id,
                "allowed_paths": ["README.md"],
                "max_sequence": 3,
            }
        ),
        encoding="utf-8",
    )
    return RepositoryAlias.load(alias, config_path), repo


def composer(
    tmp_path: Path,
    *,
    include_other: bool = False,
) -> tuple[NativeActionComposer, Path, Path]:
    sample, sample_repo = configured_alias(
        tmp_path,
        alias="sample",
        repository_id="sample-repository",
    )
    repositories = {"sample": sample}
    if include_other:
        other, _ = configured_alias(
            tmp_path,
            alias="other",
            repository_id="other-repository",
        )
        repositories["other"] = other
    store_path = tmp_path / "native-sessions.json"
    native = NativeActionComposer(
        repositories,
        NativeSessionStore(store_path, writer=writer),
        now_fn=lambda: NOW,
    )
    return native, store_path, sample_repo


def action(
    *,
    session_id: str,
    sequence: int,
    repo_alias: str = "sample",
    repair_correlation: object = None,
    include: bool = True,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "bdb-action-v1",
        "repo_alias": repo_alias,
        "session_id": session_id,
        "sequence": sequence,
        "operation": "open_read",
        "expected_revision": 0,
        "payload": {"path": "README.md"},
    }
    if include:
        value["repair_correlation"] = repair_correlation
    return value


def explicit_initial(correlation_id: str = CORRELATION_ID) -> dict[str, object]:
    return {
        "schema": REPAIR_CORRELATION_SCHEMA,
        "correlation_id": correlation_id,
        "role": "initial",
        "predecessor_session_id": None,
    }


def explicit_repair(
    correlation_id: str = CORRELATION_ID,
    predecessor_session_id: str = INITIAL_SESSION,
) -> dict[str, object]:
    return {
        "schema": REPAIR_CORRELATION_SCHEMA,
        "correlation_id": correlation_id,
        "role": "repair",
        "predecessor_session_id": predecessor_session_id,
    }


def bind_initial(native: NativeActionComposer, correlation_id: str = CORRELATION_ID) -> None:
    native.compose(
        action(
            session_id=INITIAL_SESSION,
            sequence=1,
            repair_correlation=explicit_initial(correlation_id),
        )
    )


def test_native_action_persists_and_reuses_semantically_valid_repair_correlation(tmp_path: Path) -> None:
    native, store_path, _ = composer(tmp_path)
    bind_initial(native)

    _, first = native.compose(
        action(
            session_id=REPAIR_SESSION,
            sequence=1,
            repair_correlation=explicit_repair(),
        )
    )
    _, second = native.compose(
        action(session_id=REPAIR_SESSION, sequence=2, include=False)
    )

    assert first["manifest"]["repair_correlation"] == explicit_repair()
    assert second["manifest"]["repair_correlation"] == explicit_repair()
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored["sessions"][INITIAL_SESSION]["repair_correlation"] == explicit_initial()
    assert stored["sessions"][REPAIR_SESSION]["repair_correlation"] == explicit_repair()
    records = native.session_store.find_by_correlation(CORRELATION_ID)
    assert {record.session_id for record in records} == {INITIAL_SESSION, REPAIR_SESSION}


def test_native_action_rejects_correlation_change_within_session(tmp_path: Path) -> None:
    native, _, _ = composer(tmp_path)
    bind_initial(native)
    native.compose(
        action(
            session_id=REPAIR_SESSION,
            sequence=1,
            repair_correlation=explicit_repair(),
        )
    )

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                session_id=REPAIR_SESSION,
                sequence=2,
                repair_correlation=explicit_repair(OTHER_CORRELATION_ID),
            )
        )
    assert error.value.code == "journal_conflict"


def test_repair_requires_existing_predecessor(tmp_path: Path) -> None:
    native, store_path, _ = composer(tmp_path)

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                session_id=REPAIR_SESSION,
                sequence=1,
                repair_correlation=explicit_repair(),
            )
        )

    assert error.value.code == "invalid_payload"
    assert "not bound" in error.value.message
    assert not store_path.exists()


def test_repair_requires_matching_predecessor_correlation(tmp_path: Path) -> None:
    native, _, _ = composer(tmp_path)
    bind_initial(native)

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                session_id=REPAIR_SESSION,
                sequence=1,
                repair_correlation=explicit_repair(OTHER_CORRELATION_ID),
            )
        )

    assert error.value.code == "invalid_payload"
    assert "does not match" in error.value.message


def test_second_initial_with_same_correlation_is_rejected(tmp_path: Path) -> None:
    native, _, _ = composer(tmp_path)
    bind_initial(native)

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                session_id=SECOND_INITIAL_SESSION,
                sequence=1,
                repair_correlation=explicit_initial(),
            )
        )

    assert error.value.code == "journal_conflict"
    assert "already has" in error.value.message


def test_repair_predecessor_must_belong_to_same_repository_alias(tmp_path: Path) -> None:
    native, _, _ = composer(tmp_path, include_other=True)
    bind_initial(native)

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                session_id=REPAIR_SESSION,
                sequence=1,
                repo_alias="other",
                repair_correlation=explicit_repair(),
            )
        )

    assert error.value.code == "policy_denied"
    assert "different repository alias" in error.value.message


def test_uncorrelated_native_session_remains_backward_compatible(tmp_path: Path) -> None:
    native, store_path, _ = composer(tmp_path)

    _, envelope = native.compose(
        action(session_id=REPAIR_SESSION, sequence=1, include=False)
    )

    assert "repair_correlation" not in envelope["manifest"]
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert "repair_correlation" not in stored["sessions"][REPAIR_SESSION]


def test_dirty_repo_rejects_correlated_initial_before_store_write(tmp_path: Path) -> None:
    native, store_path, repo = composer(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    assert git(repo, "status", "--porcelain=v1")

    with pytest.raises(BridgeError) as error:
        native.compose(
            action(
                session_id=INITIAL_SESSION,
                sequence=1,
                repair_correlation=explicit_initial(),
            )
        )
    assert error.value.code == "dirty_source_checkout"
    assert not store_path.exists()
