from __future__ import annotations

import os
import time
import threading
from typing import Any, Callable

from .models import (
    BridgeCycleReport,
    ServiceRunOutcome,
    ServiceInstanceState,
    ServiceStatus,
    ServiceStatusSnapshot,
    BridgeErrorCode,
    IngestionReport,
    PollReport,
)
from .protocol import BridgeError
from .journal import Journal
from .ingestion import CommandIngestor
from .scheduler import SingleQueueScheduler
from .result_outbox import OutboxProcessor, ResultCoordinator, OutboxProcessState
from .instance_lock import InstanceLock
from .heartbeat import HeartbeatWorker
from .execution import SystemCrash


def is_fatal_error(exc: Exception) -> bool:
    if isinstance(exc, SystemCrash):
        return True
    if isinstance(exc, BridgeError):
        if exc.code in (
            BridgeErrorCode.TRANSPORT_UNAVAILABLE.value,
            BridgeErrorCode.GIT_ERROR.value,
        ):
            return False
        return True
    return True


class BridgeService:
    def __init__(
        self,
        config: Any,
        journal: Journal,
        ingestor: CommandIngestor,
        scheduler: SingleQueueScheduler,
        result_coordinator: ResultCoordinator,
        outbox_processor: OutboxProcessor,
        instance_lock: InstanceLock,
        *,
        clock: Callable[[], str] | None = None,
        waiter: threading.Event | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.ingestor = ingestor
        self.scheduler = scheduler
        self.result_coordinator = result_coordinator
        self.outbox_processor = outbox_processor
        self.instance_lock = instance_lock
        self.clock = clock or journal._now_fn
        self.waiter = waiter or threading.Event()
        self.fault_hook = fault_hook
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True
        self.waiter.set()

    def _fault(self, point: str) -> None:
        if self.fault_hook:
            self.fault_hook(point)

    def _should_stop(self, instance_id: str) -> bool:
        if self._stop_requested:
            return True
        inst = self.journal.get_service_instance(instance_id)
        if inst and inst.state == ServiceInstanceState.STOPPING:
            return True
        return False

    def _claim_next(self, instance_id: str):
        try:
            return self.scheduler.claim_next(service_instance_id=instance_id)
        except TypeError as exc:
            message = str(exc)
            if "unexpected keyword argument 'service_instance_id'" not in message:
                raise
            return self.scheduler.claim_next()

    @staticmethod
    def _cycle_made_progress(report: BridgeCycleReport) -> bool:
        if (
            report.recovery_outcome
            and report.recovery_outcome.startswith("recovered:")
            and not report.recovery_outcome.endswith(":result_staged")
        ):
            return True
        if report.outbox_outcome in {
            f"processed:{OutboxProcessState.PUBLISHED.value}",
            f"processed:{OutboxProcessState.ALREADY_PUBLISHED.value}",
            f"processed:{OutboxProcessState.COLLISION.value}",
        }:
            return True
        if report.ingest_outcome and report.ingest_outcome.startswith("ingested:"):
            return True
        if (
            report.execute_outcome
            and report.execute_outcome.startswith("executed:")
            and not report.execute_outcome.endswith(":result_staged")
        ):
            return True
        return False

    def run_cycle(self, instance_id: str) -> BridgeCycleReport:
        t0 = time.perf_counter()

        recovery_outcome = None
        outbox_outcome = None
        ingest_outcome = None
        execute_outcome = None

        if self._should_stop(instance_id):
            return BridgeCycleReport(None, None, None, None, 0.0)

        # Faza 1: Recovery
        rec_cmd = self.journal.get_recoverable_command()
        if rec_cmd is not None:
            try:
                outcome = self.result_coordinator.process(rec_cmd.command_id)
                recovery_outcome = f"recovered:{outcome.command_state}"
            except Exception as exc:
                if is_fatal_error(exc):
                    raise
                recovery_outcome = f"failed:{type(exc).__name__}"
        else:
            recovery_outcome = "none"
        self._fault("AFTER_RECOVERY_PHASE")

        if self._should_stop(instance_id):
            dt = (time.perf_counter() - t0) * 1000.0
            return BridgeCycleReport(recovery_outcome, outbox_outcome, ingest_outcome, execute_outcome, dt)

        # Faza 2: Pending Outbox
        try:
            pub_outcome = self.outbox_processor.process_one_due()
            if pub_outcome.state != OutboxProcessState.NO_DUE:
                outbox_outcome = f"processed:{pub_outcome.state.value}"
            else:
                outbox_outcome = "none"
        except Exception as exc:
            if is_fatal_error(exc):
                raise
            outbox_outcome = f"failed:{type(exc).__name__}"
        self._fault("AFTER_OUTBOX_PHASE")

        if self._should_stop(instance_id):
            dt = (time.perf_counter() - t0) * 1000.0
            return BridgeCycleReport(recovery_outcome, outbox_outcome, ingest_outcome, execute_outcome, dt)

        # Faza 3: Ingest
        try:
            poll_report = self.ingestor.poll_once()
            if not isinstance(poll_report, PollReport):
                # Fail closed for malformed test doubles or connector adapters:
                # unknown objects must not be interpreted as durable progress.
                ingest_outcome = "none"
            elif isinstance(poll_report.ingestion, IngestionReport):
                ingestion = poll_report.ingestion
                work_count = (
                    ingestion.manifests_recorded
                    + ingestion.commands_discovered
                    + ingestion.commands_validated
                    + ingestion.commands_rejected
                    + ingestion.commands_expired
                    + ingestion.issues_recorded
                )
                if work_count > 0:
                    ingest_outcome = f"ingested:{work_count}"
                elif poll_report.error_code:
                    ingest_outcome = f"error:{poll_report.error_code}"
                else:
                    ingest_outcome = "none"
            elif poll_report.error_code:
                ingest_outcome = f"error:{poll_report.error_code}"
            else:
                ingest_outcome = "none"
        except Exception as exc:
            if is_fatal_error(exc):
                raise
            ingest_outcome = f"failed:{type(exc).__name__}"
        self._fault("AFTER_INGEST_PHASE")

        if self._should_stop(instance_id):
            dt = (time.perf_counter() - t0) * 1000.0
            return BridgeCycleReport(recovery_outcome, outbox_outcome, ingest_outcome, execute_outcome, dt)

        # Faza 4: Execute
        has_blocking = self.journal.has_blocking_ingestion_issues()

        if rec_cmd is None and not has_blocking:
            cmd = self._claim_next(instance_id)
            if cmd is not None:
                self._fault("AFTER_EXECUTE_CLAIM")
                try:
                    outcome = self.result_coordinator.process(cmd.command_id)
                    execute_outcome = f"executed:{outcome.command_state}"
                except Exception as exc:
                    if is_fatal_error(exc):
                        raise
                    execute_outcome = f"failed:{type(exc).__name__}"
            else:
                execute_outcome = "none"
        else:
            execute_outcome = "skipped"
        self._fault("AFTER_EXECUTE_PHASE")

        if self._should_stop(instance_id):
            dt = (time.perf_counter() - t0) * 1000.0
            return BridgeCycleReport(
                recovery_outcome,
                outbox_outcome,
                ingest_outcome,
                execute_outcome,
                dt,
            )

        dt = (time.perf_counter() - t0) * 1000.0
        return BridgeCycleReport(
            recovery_outcome=recovery_outcome,
            outbox_outcome=outbox_outcome,
            ingest_outcome=ingest_outcome,
            execute_outcome=execute_outcome,
            cycle_time_ms=dt,
        )

    def run(self, instance_id: str) -> ServiceRunOutcome:
        now = self.clock()
        try:
            self.journal.mark_abandoned_service_instances_stale("Abandoned after process crash")
            self.journal.start_service_instance(instance_id, os.getpid(), now)
        except Exception as exc:
            return ServiceRunOutcome(instance_id, 1, f"Failed to register service instance: {exc}")

        self._fault("AFTER_SERVICE_ROW_BEFORE_HEARTBEAT")

        heartbeat = HeartbeatWorker(
            self.journal.path,
            instance_id,
            self.config.heartbeat_interval_seconds,
            now_fn=self.clock,
        )
        heartbeat.start()

        exit_code = 0
        error_msg = None

        try:
            while not self._should_stop(instance_id):
                hb_err = heartbeat.get_error()
                if hb_err is not None:
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_CONFLICT,
                        f"Heartbeat worker encountered error: {hb_err}",
                    )

                report = self.run_cycle(instance_id)

                if self._should_stop(instance_id):
                    continue
                if self._cycle_made_progress(report):
                    continue

                self._fault("BEFORE_IDLE_WAIT")
                if self._should_stop(instance_id):
                    continue
                self.waiter.wait(timeout=self.config.idle_poll_seconds)
                self.waiter.clear()

            self._fault("AFTER_STOP_REQUEST_OBSERVED")
            self._fault("BEFORE_SERVICE_STOPPED_COMMIT")

            self.journal.mark_service_instance_stopped(instance_id, exit_code=0)
        except SystemCrash:
            raise
        except Exception as exc:
            exit_code = 1
            error_msg = str(exc)
            try:
                self.journal.mark_service_instance_failed(instance_id, error_msg)
            except Exception:
                pass
        finally:
            heartbeat.stop()

        return ServiceRunOutcome(instance_id, exit_code, error_msg)
