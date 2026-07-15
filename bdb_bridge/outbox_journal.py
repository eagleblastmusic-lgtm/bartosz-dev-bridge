from __future__ import annotations

from typing import Any

from .outbox_common import get_outbox
from .outbox_publication import mark_result_collision, mark_result_published
from .outbox_retry import (
    claim_due_outbox,
    claim_outbox_command,
    list_due_outbox,
    record_outbox_failure,
)
from .outbox_staging import stage_result_and_enqueue


def install_journal_outbox_api(journal_class: type[Any]) -> None:
    journal_class.get_outbox = get_outbox
    journal_class.stage_result_and_enqueue = stage_result_and_enqueue
    journal_class.list_due_outbox = list_due_outbox
    journal_class.claim_due_outbox = claim_due_outbox
    journal_class.claim_outbox_command = claim_outbox_command
    journal_class.record_outbox_failure = record_outbox_failure
    journal_class.mark_result_published = mark_result_published
    journal_class.mark_result_collision = mark_result_collision
