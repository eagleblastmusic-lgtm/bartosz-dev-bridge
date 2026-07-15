from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bdb_bridge import (
    CommandState,
    OutboxProcessState,
    OutboxProcessor,
    PublishAttempt,
    PublishAttemptState,
    RemoteResult,
    RemoteResultState,
    sha256_bytes,
)
from tests.helpers.result_outbox_fixture import COMMAND_ID, NOW, make_journal, stage


@dataclass
class FakeTransport:
    remote: bytes | None = None
    unavailable: bool = False
    branch_moved: bool = False
    pushes: int = 0
    head: str = "e" * 40

    def fetch_results_head(self) -> str:
        if self.unavailable:
            from bdb_bridge import BridgeError
            raise BridgeError("transport_unavailable", "offline")
        return self.head

    def read_result(self, remote_path: str) -> RemoteResult:
        if self.unavailable:
            return RemoteResult(RemoteResultState.UNAVAILABLE, remote_path, None, None, None, self.head, "offline")
        if self.remote is None:
            return RemoteResult(RemoteResultState.ABSENT, remote_path, None, None, None, self.head)
        return RemoteResult(RemoteResultState.PRESENT, remote_path, self.remote, sha256_bytes(self.remote), "f" * 40, self.head)

    def publish_result(self, *, remote_path: str, content: bytes, expected_results_head: str) -> PublishAttempt:
        self.pushes += 1
        if self.branch_moved:
            return PublishAttempt(PublishAttemptState.BRANCH_MOVED, remote_path, expected_results_head, diagnostic="non-fast-forward")
        self.remote = content
        self.head = "f" * 40
        return PublishAttempt(PublishAttemptState.PUBLISHED, remote_path, expected_results_head, commit_sha=self.head, remote_sha256=sha256_bytes(content))


def test_absent_publish_and_existing_identical(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    staged, _, _ = stage(journal)
    transport = FakeTransport()
    outcome = OutboxProcessor(journal, transport, now_fn=lambda: NOW).process_command(COMMAND_ID)
    assert outcome.state == OutboxProcessState.PUBLISHED
    assert transport.remote == staged.result_bytes
    assert transport.pushes == 1
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_PUBLISHED
    assert OutboxProcessor(journal, transport, now_fn=lambda: NOW).process_command(COMMAND_ID).state == OutboxProcessState.ALREADY_PUBLISHED
    assert transport.pushes == 1
    journal.close()


def test_existing_identical_zero_push_and_collision_zero_push(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    staged, _, _ = stage(journal)
    same = FakeTransport(remote=staged.result_bytes)
    assert OutboxProcessor(journal, same, now_fn=lambda: NOW).process_command(COMMAND_ID).state == OutboxProcessState.PUBLISHED
    assert same.pushes == 0
    journal.close()

    other_path = tmp_path / "other"
    other_path.mkdir()
    journal = make_journal(other_path)
    stage(journal)
    different = FakeTransport(remote=b"different")
    assert OutboxProcessor(journal, different, now_fn=lambda: NOW).process_command(COMMAND_ID).state == OutboxProcessState.COLLISION
    assert different.pushes == 0
    journal.close()


def test_unavailable_and_branch_moved_persist_single_attempt(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    stage(journal)
    offline = FakeTransport(unavailable=True)
    outcome = OutboxProcessor(journal, offline, now_fn=lambda: NOW).process_command(COMMAND_ID)
    assert outcome.state == OutboxProcessState.RETRY_SCHEDULED
    assert journal.get_outbox(COMMAND_ID).attempt_count == 1
    assert journal.get_command(COMMAND_ID).state == CommandState.RESULT_STAGED
    journal.close()

    other = tmp_path / "branch"
    other.mkdir()
    journal = make_journal(other)
    stage(journal)
    moved = FakeTransport(branch_moved=True)
    outcome = OutboxProcessor(journal, moved, now_fn=lambda: NOW).process_command(COMMAND_ID)
    assert outcome.state == OutboxProcessState.RETRY_SCHEDULED
    assert moved.pushes == 1
    assert journal.get_outbox(COMMAND_ID).attempt_count == 1
    journal.close()


def test_malformed_transport_outcomes_are_persisted_failures(tmp_path: Path) -> None:
    class Broken(FakeTransport):
        def read_result(self, remote_path: str) -> RemoteResult:
            return RemoteResult(RemoteResultState.PRESENT, remote_path, None, None, None, self.head)
    journal = make_journal(tmp_path)
    stage(journal)
    outcome = OutboxProcessor(journal, Broken(), now_fn=lambda: NOW).process_command(COMMAND_ID)
    assert outcome.state == OutboxProcessState.RETRY_SCHEDULED
    assert journal.get_outbox(COMMAND_ID).attempt_count == 1
    journal.close()


def test_processor_source_contains_no_sleep() -> None:
    import inspect
    from bdb_bridge.result_outbox import OutboxProcessor as Subject
    source = inspect.getsource(Subject)
    assert "sleep(" not in source
