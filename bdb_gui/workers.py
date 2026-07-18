from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .bootstrap import BootstrapService
from .state import BootstrapSnapshot


class BootstrapWorkerSignals(QObject):
    completed = Signal(object)


class BootstrapWorker(QRunnable):
    """Runs the read-only bootstrap outside the Qt GUI thread."""

    def __init__(self, service: BootstrapService, workspaces_root: str) -> None:
        super().__init__()
        self._service = service
        self._workspaces_root = workspaces_root
        self.signals = BootstrapWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            snapshot = self._service.load(self._workspaces_root)
        except Exception as error:  # defensive thread boundary
            snapshot = BootstrapSnapshot.failure(
                workspaces_root=self._workspaces_root,
                error_code="gui_bootstrap_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(snapshot)
