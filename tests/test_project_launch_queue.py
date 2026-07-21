from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from bdb_bridge.project_launch import ProjectLaunchQueue


UTC = timezone.utc


def test_enqueue_claim_acknowledge_and_single_owner(tmp_path) -> None:
    now = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)
    queue = ProjectLaunchQueue(tmp_path / "launch.json", now_fn=lambda: now)
    first_claim = str(uuid.uuid4())
    competing_claim = str(uuid.uuid4())

    launch = queue.enqueue(
        repo_alias="calculator",
        prompt="Create a calculator",
        auto_send=True,
        ttl_minutes=10,
    )

    assert queue.peek() == launch
    assert queue.claim(launch_id=launch.launch_id, claim_id=first_claim) == launch
    assert queue.claim(launch_id=launch.launch_id, claim_id=first_claim) == launch
    assert queue.claim(launch_id=launch.launch_id, claim_id=competing_claim) is None
    assert queue.acknowledge(launch.launch_id, competing_claim) is False
    assert queue.acknowledge(launch.launch_id, first_claim) is True
    assert queue.peek() is None


def test_expired_claim_can_be_reclaimed_by_another_tab(tmp_path) -> None:
    current = [datetime(2026, 7, 21, 3, 0, tzinfo=UTC)]
    queue = ProjectLaunchQueue(tmp_path / "launch.json", now_fn=lambda: current[0])
    launch = queue.enqueue(repo_alias="alpha", prompt="Do work", auto_send=False, ttl_minutes=5)
    first_claim = str(uuid.uuid4())
    second_claim = str(uuid.uuid4())

    assert queue.claim(
        launch_id=launch.launch_id,
        claim_id=first_claim,
        lease_seconds=5,
    ) == launch
    current[0] += timedelta(seconds=6)

    assert queue.claim(
        launch_id=launch.launch_id,
        claim_id=second_claim,
        lease_seconds=5,
    ) == launch
    assert queue.acknowledge(launch.launch_id, second_claim) is True


def test_expired_launch_is_removed(tmp_path) -> None:
    current = [datetime(2026, 7, 21, 3, 0, tzinfo=UTC)]
    queue = ProjectLaunchQueue(tmp_path / "launch.json", now_fn=lambda: current[0])
    launch = queue.enqueue(repo_alias="alpha", prompt="Do work", auto_send=False, ttl_minutes=1)
    queue.claim(launch_id=launch.launch_id, claim_id=str(uuid.uuid4()))

    current[0] += timedelta(minutes=2)

    assert queue.peek() is None
    document = (tmp_path / "launch.json").read_text(encoding="utf-8")
    assert '"pending": null' in document
    assert '"claim": null' in document


def test_enqueue_refuses_to_overwrite_pending_prompt(tmp_path) -> None:
    queue = ProjectLaunchQueue(tmp_path / "launch.json")
    queue.enqueue(repo_alias="alpha", prompt="First", auto_send=True)

    with pytest.raises(ValueError, match="already contains"):
        queue.enqueue(repo_alias="beta", prompt="Second", auto_send=True)


@pytest.mark.parametrize(
    ("alias", "prompt", "auto_send", "ttl"),
    [
        ("../escape", "ok", True, 10),
        ("alpha", "", True, 10),
        ("alpha", "ok", "yes", 10),
        ("alpha", "ok", True, 0),
        ("alpha", "ok", True, 61),
    ],
)
def test_enqueue_rejects_unsafe_values(tmp_path, alias, prompt, auto_send, ttl) -> None:
    queue = ProjectLaunchQueue(tmp_path / "launch.json")

    with pytest.raises(ValueError):
        queue.enqueue(
            repo_alias=alias,
            prompt=prompt,
            auto_send=auto_send,
            ttl_minutes=ttl,
        )


def test_queue_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text(
        '{"schema":"bdb-project-launch-queue-v1","pending":null,"claim":null}',
        encoding="utf-8",
    )
    link = tmp_path / "launch.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    queue = ProjectLaunchQueue(link)
    with pytest.raises(ValueError, match="regular file"):
        queue.peek()
