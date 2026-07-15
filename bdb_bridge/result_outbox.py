from __future__ import annotations

from typing import Any, Callable

from .execution import ExecutionCoordinator
from .journal import Journal
from .models import (
    BridgeErrorCode,
    CommandState,
    OutboxProcessOutcome,
    OutboxProcessState,
    OutboxRecord,
    OutboxState,
    PublishAttemptState,
    RemoteResultState,
    ResultCoordinationOutcome,
)
from .protocol import BridgeError
from .result_staging import ResultBuildInput, ResultStager
from .result_transport import ResultTransport

FaultHook = Callable[[str], None]
Clock = Callable[[], str]


class OutboxProcessor:
    def __init__(
        self,
        journal: Journal,
        transport: ResultTransport,
        *,
        now_fn: Clock | None = None,
        fault_hook: FaultHook | None = None,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        lease_seconds: float = 60.0,
    ) -> None:
        self.journal = journal
        self.transport = transport
        self.now_fn = now_fn or journal._now_fn
        self.fault_hook = fault_hook
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.lease_seconds = lease_seconds

    def _fault(self, point: str) -> None:
        if self.fault_hook:
            self.fault_hook(point)

    def _outcome(
        self,
        state: OutboxProcessState,
        record: OutboxRecord | None,
        *,
        published_commit_sha: str | None = None,
        diagnostic: str | None = None,
    ) -> OutboxProcessOutcome:
        return OutboxProcessOutcome(
            state=state,
            command_id=record.command_id if record else None,
            attempt_count=record.attempt_count if record else None,
            next_attempt_at=record.next_attempt_at if record else None,
            published_commit_sha=published_commit_sha,
            diagnostic=diagnostic,
        )

    def process_one_due(self) -> OutboxProcessOutcome:
        now = self.now_fn()
        claimed = self.journal.claim_due_outbox(now, lease_seconds=self.lease_seconds)
        if claimed is None:
            return self._outcome(OutboxProcessState.NO_DUE, None)
        return self._process_claimed(claimed, now=now)

    def process_command(self, command_id: str) -> OutboxProcessOutcome:
        command = self.journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command not found: {command_id}")
        record = self.journal.get_outbox(command_id)
        if command.state == CommandState.RESULT_PUBLISHED:
            return self._outcome(OutboxProcessState.ALREADY_PUBLISHED, record, published_commit_sha=record.published_commit_sha if record else None)
        if command.state == CommandState.MANUAL_RECONCILIATION_REQUIRED:
            return self._outcome(OutboxProcessState.COLLISION, record, diagnostic="manual reconciliation required")
        if command.state != CommandState.RESULT_STAGED:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, f"Outbox processing requires RESULT_STAGED, got {command.state.value}")
        now = self.now_fn()
        claimed = self.journal.claim_outbox_command(command_id, now, lease_seconds=self.lease_seconds)
        if claimed is None:
            current = self.journal.get_outbox(command_id)
            if current is not None and current.state == OutboxState.PENDING:
                reconciled = self._reconcile_without_push(current, now=now)
                if reconciled is not None:
                    return reconciled
            return self._outcome(OutboxProcessState.RETRY_SCHEDULED, current, diagnostic="outbox entry is not due or is claimed by another processor")
        if claimed.state == OutboxState.PUBLISHED:
            return self._outcome(OutboxProcessState.ALREADY_PUBLISHED, claimed, published_commit_sha=claimed.published_commit_sha)
        if claimed.state == OutboxState.COLLISION:
            return self._outcome(OutboxProcessState.COLLISION, claimed, diagnostic=claimed.last_error)
        return self._process_claimed(claimed, now=now)

    def _reconcile_without_push(self, record: OutboxRecord, *, now: str) -> OutboxProcessOutcome | None:
        result = self.journal.get_result(record.command_id)
        if result is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Outbox entry has no staged result")
        try:
            content = result.result_json.encode("utf-8", errors="strict")
            head = self.transport.fetch_results_head()
        except (UnicodeEncodeError, BridgeError):
            return None
        remote = self.transport.read_result(record.remote_path)
        if remote.state != RemoteResultState.PRESENT or remote.content is None or remote.content_sha256 is None:
            return None
        if remote.content == content and remote.content_sha256 == record.result_sha256:
            return self._published(record, remote_hash=remote.content_sha256, commit_sha=remote.commit_sha or head, now=now)
        return self._collision(record, observed_hash=remote.content_sha256, diagnostic="remote result path contains different exact bytes during recovery reconciliation")

    def _failure(self, claimed: OutboxRecord, *, now: str, diagnostic: str) -> OutboxProcessOutcome:
        updated = self.journal.record_outbox_failure(
            claimed.command_id,
            expected_attempt_count=claimed.attempt_count,
            error_message=diagnostic,
            now=now,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
        )
        return self._outcome(OutboxProcessState.RETRY_SCHEDULED, updated, diagnostic=updated.last_error)

    def _collision(self, claimed: OutboxRecord, *, observed_hash: str, diagnostic: str) -> OutboxProcessOutcome:
        updated = self.journal.mark_result_collision(
            claimed.command_id,
            observed_result_sha256=observed_hash,
            diagnostic=diagnostic,
            fault_hook=self.fault_hook,
        )
        return self._outcome(OutboxProcessState.COLLISION, updated, diagnostic=updated.last_error)

    def _published(self, claimed: OutboxRecord, *, remote_hash: str, commit_sha: str, now: str) -> OutboxProcessOutcome:
        updated = self.journal.mark_result_published(
            claimed.command_id,
            remote_result_sha256=remote_hash,
            published_commit_sha=commit_sha,
            published_at=now,
            fault_hook=self.fault_hook,
        )
        return self._outcome(OutboxProcessState.PUBLISHED, updated, published_commit_sha=updated.published_commit_sha)

    def _process_claimed(self, claimed: OutboxRecord, *, now: str) -> OutboxProcessOutcome:
        result = self.journal.get_result(claimed.command_id)
        if result is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Outbox entry has no staged result")
        try:
            content = result.result_json.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Persisted result is not strict UTF-8") from exc
        if result.result_sha256 != claimed.result_sha256:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Result/outbox hash mismatch")

        try:
            head = self.transport.fetch_results_head()
        except Exception as exc:
            return self._failure(claimed, now=now, diagnostic=f"fetch unavailable: {type(exc).__name__}")
        try:
            remote = self.transport.read_result(claimed.remote_path)
        except Exception as exc:
            return self._failure(claimed, now=now, diagnostic=f"read unavailable: {type(exc).__name__}")
        if remote.state == RemoteResultState.UNAVAILABLE:
            return self._failure(claimed, now=now, diagnostic=f"read unavailable: {remote.diagnostic or 'unknown'}")
        if remote.state == RemoteResultState.PRESENT:
            if remote.content is None or remote.content_sha256 is None:
                return self._failure(claimed, now=now, diagnostic="malformed present remote result")
            if remote.content == content and remote.content_sha256 == claimed.result_sha256:
                return self._published(claimed, remote_hash=remote.content_sha256, commit_sha=remote.commit_sha or head, now=now)
            return self._collision(claimed, observed_hash=remote.content_sha256, diagnostic="remote result path contains different exact bytes")

        try:
            attempt = self.transport.publish_result(remote_path=claimed.remote_path, content=content, expected_results_head=head)
        except Exception as exc:
            return self._failure(claimed, now=now, diagnostic=f"publish unavailable: {type(exc).__name__}")
        if attempt.state == PublishAttemptState.COLLISION:
            if attempt.remote_sha256 is None:
                return self._failure(claimed, now=now, diagnostic="malformed collision outcome without remote hash")
            return self._collision(claimed, observed_hash=attempt.remote_sha256, diagnostic=attempt.diagnostic or "publish collision")
        if attempt.state in {PublishAttemptState.BRANCH_MOVED, PublishAttemptState.UNAVAILABLE}:
            return self._failure(claimed, now=now, diagnostic=f"transport {attempt.state.value}: {attempt.diagnostic or 'no diagnostic'}")
        if attempt.state not in {PublishAttemptState.PUBLISHED, PublishAttemptState.IDENTICAL}:
            return self._failure(claimed, now=now, diagnostic=f"unexpected publish outcome: {attempt.state.value}")

        self._fault("AFTER_REMOTE_PUSH_BEFORE_LOCAL_ACK")
        try:
            fresh_head = self.transport.fetch_results_head()
        except Exception as exc:
            return self._failure(claimed, now=now, diagnostic=f"post-push fetch unavailable: {type(exc).__name__}")
        try:
            verified = self.transport.read_result(claimed.remote_path)
        except Exception as exc:
            return self._failure(claimed, now=now, diagnostic=f"post-push read unavailable: {type(exc).__name__}")
        if verified.state == RemoteResultState.UNAVAILABLE:
            return self._failure(claimed, now=now, diagnostic=f"post-push read unavailable: {verified.diagnostic or 'unknown'}")
        if verified.state == RemoteResultState.ABSENT:
            return self._failure(claimed, now=now, diagnostic="remote result missing after alleged push")
        if verified.state == RemoteResultState.PRESENT and (verified.content is None or verified.content_sha256 is None):
            return self._failure(claimed, now=now, diagnostic="malformed post-push remote result")
        if verified.content != content or verified.content_sha256 != claimed.result_sha256:
            assert verified.content_sha256 is not None
            return self._collision(claimed, observed_hash=verified.content_sha256, diagnostic="remote bytes differ after publication attempt")
        commit_sha = attempt.commit_sha or verified.commit_sha or fresh_head
        return self._published(claimed, remote_hash=verified.content_sha256, commit_sha=commit_sha, now=now)


class ResultCoordinator:
    def __init__(
        self,
        config: Any,
        journal: Journal,
        outbox_processor: OutboxProcessor,
        *,
        now_fn: Clock | None = None,
        fault_hook: FaultHook | None = None,
        execution_factory: Callable[[Any, Journal, FaultHook | None], Any] | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.outbox_processor = outbox_processor
        self.now_fn = now_fn or journal._now_fn
        self.fault_hook = fault_hook
        self.execution_factory = execution_factory or (lambda config, journal, hook: ExecutionCoordinator(config, journal, fault_hook=hook))
        self.stager = ResultStager(journal)

    def _fault(self, point: str) -> None:
        if self.fault_hook:
            self.fault_hook(point)

    def process(self, command_id: str) -> ResultCoordinationOutcome:
        command = self.journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command not found: {command_id}")
        if command.state == CommandState.RESULT_PUBLISHED:
            return ResultCoordinationOutcome(command_id, command.state, staged=True)
        if command.state == CommandState.RESULT_STAGED:
            publication = self.outbox_processor.process_command(command_id)
            updated = self.journal.get_command(command_id)
            assert updated is not None
            return ResultCoordinationOutcome(command_id, updated.state, staged=True, publication=publication)
        if command.state not in (CommandState.CLAIMED, CommandState.EXECUTING, CommandState.EFFECT_RECORDED):
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"Result coordination requires CLAIMED/EXECUTING/EFFECT_RECORDED/RESULT_STAGED/RESULT_PUBLISHED, got {command.state.value}",
            )

        started_at = self.now_fn()
        execution = self.execution_factory(self.config, self.journal, self.fault_hook)
        outcome = execution.execute_or_recover(command_id)
        finished_at = self.now_fn()
        command = self.journal.get_command(command_id)
        session = self.journal.get_session(command.session_id) if command else None
        plan = self.journal.get_operation_plan(command_id)
        effect = self.journal.get_operation_effect(command_id)
        if command is None or session is None or plan is None or effect is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Execution recovery did not preserve command/session/plan/effect")
        value = ResultBuildInput(session, command, plan, effect, outcome, started_at, finished_at)
        staged = self.stager.build(value)
        self._fault("AFTER_RESULT_BUILT_BEFORE_STAGE")
        self.journal.stage_result_and_enqueue(command_id=command_id, result_json=staged.result_json, remote_path=staged.remote_path, fault_hook=self.fault_hook)
        self._fault("AFTER_STAGE_COMMIT_BEFORE_PUBLISH")
        updated = self.journal.get_command(command_id)
        assert updated is not None
        return ResultCoordinationOutcome(command_id, updated.state, staged=True)
