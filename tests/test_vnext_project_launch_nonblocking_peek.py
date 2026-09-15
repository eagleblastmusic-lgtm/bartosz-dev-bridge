from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import bdb_vnext.project_launch as project_launch_module
from bdb_vnext.project_launch import (
    PROJECT_LAUNCH_LOCK_SCHEMA,
    ProjectLaunchLockInfo,
    ProjectLaunchQueueAdapter,
    ProjectLaunchQueueError,
)


UTC = timezone.utc


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_live_lock(queue: ProjectLaunchQueueAdapter, *, acquired_at: datetime) -> None:
    info = ProjectLaunchLockInfo(
        owner_token=uuid.uuid4().hex,
        pid=os.getpid(),
        acquired_at=_utc_text(acquired_at),
        stale_after_seconds=10.0,
        schema=PROJECT_LAUNCH_LOCK_SCHEMA,
    )
    queue.lock_path.write_text(
        json.dumps(info.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_peek_returns_live_launch_while_mutation_lock_is_held(tmp_path, monkeypatch) -> None:
    queue = ProjectLaunchQueueAdapter(tmp_path / "project-launch-queue.json")
    launch = queue.enqueue(
        repo_alias="premium-calculator",
        prompt="Do not send; transport regression probe.",
        auto_send=False,
    )
    _write_live_lock(queue, acquired_at=datetime.now(UTC))
    monkeypatch.setattr(project_launch_module, "_LOCK_TIMEOUT_SECONDS", 0.01)

    assert queue.peek() == launch
    assert queue.lock_path.exists()


def test_expired_peek_stays_nonblocking_when_cleanup_lock_is_held(tmp_path, monkeypatch) -> None:
    current = [datetime(2026, 9, 15, 21, 0, tzinfo=UTC)]
    queue = ProjectLaunchQueueAdapter(
        tmp_path / "project-launch-queue.json",
        now_fn=lambda: current[0],
    )
    launch = queue.enqueue(
        repo_alias="premium-calculator",
        prompt="Do not send; expiry regression probe.",
        auto_send=False,
        ttl_minutes=1,
    )
    _write_live_lock(queue, acquired_at=current[0])
    monkeypatch.setattr(project_launch_module, "_LOCK_TIMEOUT_SECONDS", 0.01)

    current[0] += timedelta(minutes=2)

    assert queue.peek() is None
    assert queue.lock_path.exists()
    assert launch.launch_id in queue.path.read_text(encoding="utf-8")


def test_expired_peek_cleans_snapshot_when_lock_is_immediately_available(tmp_path) -> None:
    current = [datetime(2026, 9, 15, 21, 0, tzinfo=UTC)]
    queue = ProjectLaunchQueueAdapter(
        tmp_path / "project-launch-queue.json",
        now_fn=lambda: current[0],
    )
    queue.enqueue(
        repo_alias="premium-calculator",
        prompt="Do not send; cleanup regression probe.",
        auto_send=False,
        ttl_minutes=1,
    )

    current[0] += timedelta(minutes=2)

    assert queue.peek() is None
    state = json.loads(queue.path.read_text(encoding="utf-8"))
    assert state["pending"] is None
    assert state["claim"] is None
    assert not queue.lock_path.exists()


def test_mutations_still_fail_closed_behind_live_lock(tmp_path, monkeypatch) -> None:
    queue = ProjectLaunchQueueAdapter(tmp_path / "project-launch-queue.json")
    _write_live_lock(queue, acquired_at=datetime.now(UTC))
    monkeypatch.setattr(project_launch_module, "_LOCK_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(ProjectLaunchQueueError) as exc_info:
        queue.enqueue(
            repo_alias="premium-calculator",
            prompt="Mutation must remain serialized.",
            auto_send=False,
        )

    assert exc_info.value.code == "queue_busy"
    assert queue.lock_path.exists()
