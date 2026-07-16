from __future__ import annotations

from .journal import Journal
from .models import CommandRecord
from .session_finalization import SessionFinalizer


class SingleQueueScheduler:
    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    def claim_next(self) -> CommandRecord | None:
        claimed = self._journal.claim_next_command()
        if claimed is not None:
            return claimed

        handoff = SessionFinalizer(self._journal).handoff_ready_session()
        if handoff is None:
            return None

        return self._journal.claim_next_command()
