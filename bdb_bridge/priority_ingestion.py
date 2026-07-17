from __future__ import annotations

from .models import IngestionReport, PollReport


def _work_count(report: PollReport) -> int:
    ingestion = report.ingestion
    if not isinstance(ingestion, IngestionReport):
        return 0
    return (
        ingestion.manifests_recorded
        + ingestion.commands_discovered
        + ingestion.commands_validated
        + ingestion.commands_rejected
        + ingestion.commands_expired
        + ingestion.issues_recorded
    )


class PriorityCommandIngestor:
    """Poll the direct local lane first and preserve Git as a fallback.

    The local and Git ingestors retain separate durable ``source_id`` rows.  A Git
    retry/backoff therefore cannot delay a locally submitted command.  Git is still
    polled when the local lane produced no durable work.
    """

    def __init__(self, local_ingestor: object, fallback_ingestor: object) -> None:
        self.local_ingestor = local_ingestor
        self.fallback_ingestor = fallback_ingestor

    def poll_once(self) -> PollReport:
        local = self.local_ingestor.poll_once()
        if local.error_code is not None or _work_count(local) > 0:
            return local
        return self.fallback_ingestor.poll_once()
