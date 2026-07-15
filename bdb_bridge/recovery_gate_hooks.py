from __future__ import annotations

from typing import Type

from .journal_ingestion import CollisionError
from .models import BridgeErrorCode, IngestionReport, PollReport
from .protocol import BridgeError, parse_strict_utc_timestamp


def install_command_ingestor_fault_hook(ingestor_cls: Type[object]) -> None:
    original_init = ingestor_cls.__init__
    if getattr(original_init, "_ghb07_wrapped", False):
        return

    def init_with_fault(self: object, *args: object, fault_hook=None, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        self._fault_hook = fault_hook

    init_with_fault._ghb07_wrapped = True

    def poll_once_with_discovered_fault(self: object) -> PollReport:
        now = self._now_fn()
        now_dt = parse_strict_utc_timestamp(now, field="now")
        source = self._journal.get_ingestion_source(self._source_id)
        if source.next_attempt_at is not None:
            next_dt = parse_strict_utc_timestamp(source.next_attempt_at, field="next_attempt_at")
            if now_dt < next_dt:
                return PollReport(False, True, False, None, None, None, None)
        try:
            snapshot = self._transport.fetch_snapshot()
        except BridgeError as exc:
            if exc.code in {BridgeErrorCode.TRANSPORT_UNAVAILABLE.value, BridgeErrorCode.GIT_ERROR.value}:
                self._journal.record_transport_failure(
                    self._source_id, str(exc), base_delay=self._backoff_base, max_delay=self._backoff_max
                )
                return PollReport(True, False, False, None, None, BridgeErrorCode.TRANSPORT_UNAVAILABLE.value, str(exc))
            raise
        except Exception as exc:
            self._journal.record_transport_failure(
                self._source_id, str(exc), base_delay=self._backoff_base, max_delay=self._backoff_max
            )
            return PollReport(True, False, False, None, None, BridgeErrorCode.TRANSPORT_UNAVAILABLE.value, str(exc))

        self._journal.record_transport_success(self._source_id, snapshot.snapshot_sha)
        try:
            try:
                ingestion = self.ingest_snapshot(snapshot)
            except CollisionError as exc:
                ingestion = exc.report
            if ingestion.commands_discovered and self._fault_hook:
                self._fault_hook("AFTER_DISCOVERED_BEFORE_VALIDATION")
            validation = self.validate_pending()
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"Unexpected error during polling: {exc}") from exc

        has_blocking = self._journal.has_blocking_ingestion_issues()
        combined = IngestionReport(
            manifests_recorded=ingestion.manifests_recorded,
            commands_discovered=ingestion.commands_discovered,
            commands_validated=validation.commands_validated,
            commands_rejected=validation.commands_rejected,
            commands_expired=validation.commands_expired,
            issues_recorded=ingestion.issues_recorded + validation.issues_recorded,
            blocking_issues=has_blocking,
        )
        return PollReport(
            transport_called=True,
            transport_skipped=False,
            transport_succeeded=True,
            snapshot_sha=snapshot.snapshot_sha,
            ingestion=combined,
            error_code=BridgeErrorCode.INGESTION_BLOCKED.value if has_blocking else None,
            error_message="Ingestion blocked due to unresolved issues" if has_blocking else None,
        )

    ingestor_cls.__init__ = init_with_fault
    ingestor_cls.poll_once = poll_once_with_discovered_fault
