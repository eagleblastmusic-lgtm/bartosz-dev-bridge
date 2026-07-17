from __future__ import annotations

from .models import BridgeErrorCode
from .protocol import BridgeError
from .result_outbox import OutboxProcessor


class LocalMirroringOutboxProcessor(OutboxProcessor):
    """Make a durable local result available before the Git publication path.

    The canonical outbox state machine and Git transport remain unchanged.  The local
    sink is an exact-byte, idempotent mirror used by Native Messaging.  A sink failure
    fails closed and leaves the command in ``RESULT_STAGED`` for recovery.
    """

    def __init__(self, *args, result_sink: object, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.result_sink = result_sink

    def _export_local(self, command_id: str) -> None:
        result = self.journal.get_result(command_id)
        outbox = self.journal.get_outbox(command_id)
        if result is None or outbox is None:
            return
        try:
            content = result.result_json.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Persisted result is not strict UTF-8",
            ) from exc
        self.result_sink.publish(outbox.remote_path, content)

    def process_command(self, command_id: str):
        self._export_local(command_id)
        return super().process_command(command_id)

    def _process_claimed(self, claimed, *, now: str):
        self._export_local(claimed.command_id)
        return super()._process_claimed(claimed, now=now)
