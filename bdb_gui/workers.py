from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .bootstrap import BootstrapService
from .current_operation import CurrentOperationService, CurrentOperationSnapshot
from .operations import ControlAction, ControlResult, ProjectOperationsService, ProjectStatusSnapshot
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


class StatusWorkerSignals(QObject):
    completed = Signal(object)


class StatusWorker(QRunnable):
    """Reads one selected project status outside the Qt GUI thread."""

    def __init__(self, service: ProjectOperationsService, workspace_root: str) -> None:
        super().__init__()
        self._service = service
        self._workspace_root = workspace_root
        self.signals = StatusWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            snapshot = self._service.read_status(self._workspace_root)
        except Exception as error:  # defensive thread boundary
            snapshot = ProjectStatusSnapshot(
                workspace_root=self._workspace_root,
                project_alias=None,
                overall_status=None,
                bridge_status=None,
                bridge_pid=None,
                bridge_pid_alive=None,
                native_armed=None,
                native_armed_until=None,
                promoter_running=None,
                promoter_pid=None,
                source_clean=None,
                source_head=None,
                operator_operation_id=f"gui-internal:{uuid4()}",
                error_code="gui_status_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(snapshot)


class ControlWorkerSignals(QObject):
    completed = Signal(object)


class ControlWorker(QRunnable):
    """Executes one explicitly confirmed process-control action."""

    def __init__(
        self,
        service: ProjectOperationsService,
        action: ControlAction,
        workspace_root: str,
        *,
        arm_minutes: int,
    ) -> None:
        super().__init__()
        self._service = service
        self._action = action
        self._workspace_root = workspace_root
        self._arm_minutes = arm_minutes
        self.signals = ControlWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.execute(
                self._action,
                self._workspace_root,
                arm_minutes=self._arm_minutes,
            )
        except Exception as error:  # defensive thread boundary
            result = ControlResult(
                action=self._action,
                workspace_root=self._workspace_root,
                project_alias=None,
                operator_operation_id=f"gui-internal:{uuid4()}",
                ok=False,
                error_code="gui_control_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(result)


class CurrentOperationWorkerSignals(QObject):
    completed = Signal(object)


class CurrentOperationWorker(QRunnable):
    """Reads the current Journal operation without mutating BDB."""

    def __init__(self, service: CurrentOperationService, workspace_root: str) -> None:
        super().__init__()
        self._service = service
        self._workspace_root = workspace_root
        self.signals = CurrentOperationWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            snapshot = self._service.read(self._workspace_root)
        except Exception as error:  # defensive thread boundary
            snapshot = CurrentOperationSnapshot(
                workspace_root=self._workspace_root,
                project_alias=None,
                generated_at=None,
                active=False,
                operation=None,
                operator_operation_id=f"gui-internal:{uuid4()}",
                error_code="gui_current_operation_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(snapshot)
