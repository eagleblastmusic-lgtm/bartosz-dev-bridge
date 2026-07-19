from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .session_history import SessionHistoryService, SessionHistorySnapshot


class SessionHistoryWorkerSignals(QObject):
    completed = Signal(object)


class SessionHistoryWorker(QRunnable):
    def __init__(
        self,
        service: SessionHistoryService,
        workspace_root: str,
        *,
        limit: int,
    ) -> None:
        super().__init__()
        self._service = service
        self._workspace_root = workspace_root
        self._limit = limit
        self.signals = SessionHistoryWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            snapshot = self._service.read(self._workspace_root, limit=self._limit)
        except Exception as error:
            snapshot = SessionHistorySnapshot.failure(
                self._workspace_root,
                operation_id=f"gui-internal:{uuid4()}",
                project_alias=None,
                error_code="gui_session_history_internal_error",
                error_message=f"{type(error).__name__}: {error}",
                limit=self._limit,
            )
        self.signals.completed.emit(snapshot)
