from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent

from .project_window import ProjectControlCenterWindow
from .projects import PrepareResult
from .workers import ControlWorker

if TYPE_CHECKING:
    from .tray import TrayController


class TrayProjectControlCenterWindow(ProjectControlCenterWindow):
    """P12 window: close-to-tray plus an explicit, confirmed exit path."""

    prepare_finished = Signal(object)

    def __init__(self, **kwargs: Any) -> None:
        self._tray_controller: TrayController | None = None
        self._force_close_requested = False
        super().__init__(**kwargs)

    def install_tray_controller(self, controller: "TrayController") -> None:
        self._tray_controller = controller

    def has_active_task(self) -> bool:
        return self._has_active_task()

    def force_close(self) -> None:
        self._force_close_requested = True
        self.close()

    def request_confirmed_stop_for_exit(self) -> bool:
        """Run the already confirmed Stop through the existing serialized worker."""
        if self._has_active_task():
            return False
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            return False
        self._set_global_busy(True, "Zatrzymywanie BDB przed zakończeniem Control Center…")
        worker = ControlWorker(
            self._operations_service,
            "stop",
            workspace_root,
            arm_minutes=self.dashboard.arm_minutes,
        )
        worker.signals.completed.connect(self._apply_control_result)
        self._control_worker = worker
        self._thread_pool.start(worker)
        return True

    def _apply_prepare_result(self, result: PrepareResult) -> None:
        super()._apply_prepare_result(result)
        self.prepare_finished.emit(result)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if (
            not self._force_close_requested
            and self._tray_controller is not None
            and self._tray_controller.handle_close(event)
        ):
            return
        super().closeEvent(event)
