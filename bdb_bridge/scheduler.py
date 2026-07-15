from __future__ import annotations

from .journal import Journal
from .models import CommandRecord


class SingleQueueScheduler:
    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    def claim_next(self) -> CommandRecord | None:
        return self._journal.claim_next_command()
