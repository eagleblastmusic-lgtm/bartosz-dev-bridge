from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import (
    M9B_NATIVE_REQUEST_SCHEMA,
    M9bNativeError,
    VNextNativeConfig,
    handle_message,
)
from bdb_vnext.project_launch_canonical import ProjectLaunchCanonicalError


PROJECT_ID = "project-0001"
BINDING_ID = "binding-0001"
CONVERSATION_ID = "conversation-0001"
LAUNCH_ID = str(uuid.uuid4())
CLAIM_ID = str(uuid.uuid4())


def _config(tmp_path: Path) -> VNextNativeConfig:
    return VNextNativeConfig(
        runtime_root=tmp_path / "vnext",
        legacy_runtime_root=tmp_path / "legacy",
        bootstrap_authority_root=tmp_path / "ProgramData" / "BDB" / "bootstrap",
    )


def _message(action: str, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": M9B_NATIVE_REQUEST_SCHEMA,
        "request_id": f"{action}-request-1",
        "action": action,
        "protocol_generation": PROTOCOL_GENERATION,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "launch_id": LAUNCH_ID,
        "claim_id": CLAIM_ID,
    }
    value.update(extra)
    return value


def _ack_message(**extra: object) -> dict[str, object]:
    return _message("project_launch_ack", conversation_id=CONVERSATION_ID, **extra)


def _claim_message(**extra: object) -> dict[str, object]:
    return _message("project_launch_claim", **extra)


def _launch() -> SimpleNamespace:
    data = {
        "launch_id": LAUNCH_ID,
        "repo_alias": "premium-calculator",
        "prompt": "Continue P3-02",
        "auto_send": False,
        "created_at": "2026-09-16T10:00:00.000000Z",
        "expires_at": "2026-09-16T10:10:00.000000Z",
        "project_id": PROJECT_ID,
        "plan_version": "1",
        "task_id": "P3-02",
        "execution_binding_id": BINDING_ID,
        "correlation_id": "corr-0001",
        "command_id": "command-0001",
        "expected_repo_head_before": "7" * 40,
    }
    launch = SimpleNamespace(**data)
    launch.to_dict = lambda: dict(data)
    return launch


class _Queue:
    def __init__(self, launch, sequence: list[str]) -> None:
        self.launch = launch
        self.sequence = sequence
        self.removed = False

    def peek(self):
        return None if self.removed else self.launch

    def claim(self, *, launch_id: str, claim_id: str, lease_seconds: int):
        assert launch_id == LAUNCH_ID
        assert claim_id == CLAIM_ID
        assert lease_seconds == 30
        self.sequence.append("queue_claim")
        return None if self.removed else self.launch

    def claim_matches(self, *, launch_id: str, claim_id: str) -> bool:
        return not self.removed and launch_id == LAUNCH_ID and claim_id == CLAIM_ID

    def acknowledge(self, *, launch_id: str, claim_id: str) -> bool:
        assert launch_id == LAUNCH_ID
        assert claim_id == CLAIM_ID
        self.sequence.append("queue_ack")
        self.removed = True
        return True


class _Canonical:
    def __init__(
        self,
        _runtime_root,
        *,
        sequence: list[str],
        fail: bool = False,
        acknowledged: bool = False,
    ) -> None:
        self.sequence = sequence
        self.fail = fail
        self.acknowledged = acknowledged

    @staticmethod
    def is_canonical_launch(_launch) -> bool:
        return True

    def is_acknowledged(self, launch) -> bool:
        assert launch.launch_id == LAUNCH_ID
        self.sequence.append("canonical_read_ack")
        return self.acknowledged

    def activate_claimed(self, launch) -> None:
        assert launch.launch_id == LAUNCH_ID
        self.sequence.append("canonical_activate")

    def acknowledge_delivery(self, launch, conversation_id: str) -> None:
        assert launch.launch_id == LAUNCH_ID
        assert conversation_id == CONVERSATION_ID
        self.sequence.append("canonical_ack")
        if self.fail:
            raise ProjectLaunchCanonicalError("canonical_ack_failed", "injected canonical failure")


def test_claim_consumes_stale_queue_projection_after_durable_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[str] = []
    launch = _launch()
    queue = _Queue(launch, sequence)

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: queue)
    monkeypatch.setattr(
        "bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState",
        lambda _root: _Canonical(_root, sequence=sequence, acknowledged=True),
    )

    response = handle_message(_config(tmp_path), _claim_message())

    assert response["status"] == "already_acknowledged"
    assert response["launch"] is None
    assert sequence == ["queue_claim", "canonical_read_ack", "queue_ack"]
    assert queue.peek() is None


def test_claim_reactivates_exact_unacknowledged_binding_before_browser_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[str] = []
    launch = _launch()
    queue = _Queue(launch, sequence)

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: queue)
    monkeypatch.setattr(
        "bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState",
        lambda _root: _Canonical(_root, sequence=sequence, acknowledged=False),
    )

    response = handle_message(_config(tmp_path), _claim_message())

    assert response["status"] == "claimed"
    assert response["launch"]["launch_id"] == LAUNCH_ID
    assert sequence == ["queue_claim", "canonical_read_ack", "canonical_activate"]
    assert queue.peek() is launch


def test_auto_send_false_ack_is_durable_before_transport_queue_is_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[str] = []
    launch = _launch()
    queue = _Queue(launch, sequence)

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: queue)
    monkeypatch.setattr(
        "bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState",
        lambda _root: _Canonical(_root, sequence=sequence),
    )

    response = handle_message(_config(tmp_path), _ack_message())

    assert response["status"] == "acknowledged"
    assert sequence == ["canonical_ack", "queue_ack"]
    assert queue.peek() is None


def test_canonical_ack_failure_leaves_transport_launch_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[str] = []
    launch = _launch()
    queue = _Queue(launch, sequence)

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: queue)
    monkeypatch.setattr(
        "bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState",
        lambda _root: _Canonical(_root, sequence=sequence, fail=True),
    )

    with pytest.raises(M9bNativeError) as exc:
        handle_message(_config(tmp_path), _ack_message())

    assert exc.value.code == "canonical_ack_failed"
    assert sequence == ["canonical_ack"]
    assert queue.peek() is launch


def test_sent_handoff_stays_additional_and_precedes_durable_delivery_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[str] = []
    launch = _launch()
    launch.auto_send = True
    queue = _Queue(launch, sequence)

    class _Coordinator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def binding(self, project_id: str, binding_id: str):
            assert project_id == PROJECT_ID
            assert binding_id == BINDING_ID
            return SimpleNamespace(launch_id=LAUNCH_ID, conversation_id=None)

        def mark_launch_handoff_sent(self, project_id: str, *, execution_binding_id: str, launch_id: str, conversation_id: str):
            assert project_id == PROJECT_ID
            assert execution_binding_id == BINDING_ID
            assert launch_id == LAUNCH_ID
            assert conversation_id == CONVERSATION_ID
            sequence.append("handoff_sent")
            return {"status": "SENT"}

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: queue)
    monkeypatch.setattr(
        "bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState",
        lambda _root: _Canonical(_root, sequence=sequence),
    )
    monkeypatch.setattr("bdb_vnext.m9b_native_host.ProjectExecutionCoordinator", _Coordinator)
    monkeypatch.setattr("bdb_vnext.m9b_native_host.ProjectCatalog", lambda _root: object())

    response = handle_message(
        _config(tmp_path),
        _ack_message(
            handoff_status="SENT",
            project_id=PROJECT_ID,
            execution_binding_id=BINDING_ID,
        ),
    )

    assert response["status"] == "acknowledged"
    assert sequence == ["handoff_sent", "canonical_ack", "queue_ack"]


def test_manual_ack_without_conversation_id_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[str] = []
    launch = _launch()
    queue = _Queue(launch, sequence)

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: queue)
    monkeypatch.setattr(
        "bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState",
        lambda _root: _Canonical(_root, sequence=sequence),
    )

    with pytest.raises(M9bNativeError) as exc:
        handle_message(_config(tmp_path), _message("project_launch_ack"))

    assert exc.value.code == "execution_conversation_invalid"
    assert sequence == []
    assert queue.peek() is launch


def test_manual_ack_with_conversation_id_succeeds_and_clears_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[str] = []
    launch = _launch()
    queue = _Queue(launch, sequence)

    monkeypatch.setattr("bdb_vnext.m9b_native_host._project_launch_queue", lambda _root: queue)
    monkeypatch.setattr(
        "bdb_vnext.m9b_native_host.ProjectLaunchCanonicalState",
        lambda _root: _Canonical(_root, sequence=sequence),
    )

    response = handle_message(_config(tmp_path), _message("project_launch_ack", conversation_id=CONVERSATION_ID))

    assert response["status"] == "acknowledged"
    assert sequence == ["canonical_ack", "queue_ack"]
    assert queue.peek() is None

