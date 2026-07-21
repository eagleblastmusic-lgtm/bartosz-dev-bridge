from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bdb_bridge.project_launch import ProjectLaunchQueue


UTC = timezone.utc


def test_enqueue_peek_and_acknowledge(tmp_path) -> None:
    now = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)
    queue = ProjectLaunchQueue(tmp_path / "launch.json", now_fn=lambda: now)

    launch = queue.enqueue(
        repo_alias="calculator",
        prompt="Create a calculator",
        auto_send=True,
        ttl_minutes=10,
    )

    assert queue.peek() == launch
    assert queue.acknowledge(launch.launch_id) is True
    assert queue.peek() is None
    assert queue.acknowledge(launch.launch_id) is False


def test_expired_launch_is_removed(tmp_path) -> None:
    current = [datetime(2026, 7, 21, 3, 0, tzinfo=UTC)]
    queue = ProjectLaunchQueue(tmp_path / "launch.json", now_fn=lambda: current[0])
    queue.enqueue(repo_alias="alpha", prompt="Do work", auto_send=False, ttl_minutes=1)

    current[0] += timedelta(minutes=2)

    assert queue.peek() is None
    assert '"pending": null' in (tmp_path / "launch.json").read_text(encoding="utf-8")


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
    target.write_text('{"schema":"bdb-project-launch-queue-v1","pending":null}', encoding="utf-8")
    link = tmp_path / "launch.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    queue = ProjectLaunchQueue(link)
    with pytest.raises(ValueError, match="regular file"):
        queue.peek()
