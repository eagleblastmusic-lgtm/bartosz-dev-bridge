from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .bootstrap import BootstrapService
from .current_operation import CurrentOperationService, CurrentOperationSnapshot
from .diagnostics import DiagnosticsExporter, DiagnosticsService, DiagnosticsSnapshot
from .diagnostics_tasks import DiagnosticsExportOutcome
from .history import HistoryService, HistorySnapshot
from .operations import ControlAction, ControlResult, ProjectOperationsService, ProjectStatusSnapshot
from .state import BootstrapSnapshot


class BootstrapWorkerSignals(QObject):
    completed = Signal(object)


class BootstrapWorker(QRunnable):
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
        except Exception as error:
            snapshot = BootstrapSnapshot.failure(
                workspaces_root=self._workspaces_root,
                error_code="gui_bootstrap_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(snapshot)


class StatusWorkerSignals(QObject):
    completed = Signal(object)


class StatusWorker(QRunnable):
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
        except Exception as error:
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
        except Exception as error:
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
        except Exception as error:
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


class HistoryWorkerSignals(QObject):
    completed = Signal(object, bool)


class HistoryWorker(QRunnable):
    def __init__(
        self,
        service: HistoryService,
        workspace_root: str,
        *,
        after_event_id: int,
        limit: int,
        session_id: str | None,
        command_id: str | None,
        append: bool,
    ) -> None:
        super().__init__()
        self._service = service
        self._workspace_root = workspace_root
        self._after_event_id = after_event_id
        self._limit = limit
        self._session_id = session_id
        self._command_id = command_id
        self._append = append
        self.signals = HistoryWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            snapshot = self._service.read(
                self._workspace_root,
                after_event_id=self._after_event_id,
                limit=self._limit,
                session_id=self._session_id,
                command_id=self._command_id,
            )
        except Exception as error:
            snapshot = HistorySnapshot.failure(
                self._workspace_root,
                operation_id=f"gui-internal:{uuid4()}",
                project_alias=None,
                error_code="gui_history_internal_error",
                error_message=f"{type(error).__name__}: {error}",
                after_event_id=self._after_event_id,
                session_id=self._session_id,
                command_id=self._command_id,
            )
        self.signals.completed.emit(snapshot, self._append)


class DiagnosticsCollectWorkerSignals(QObject):
    completed = Signal(object)


class DiagnosticsCollectWorker(QRunnable):
    """Collects one bounded read-only diagnostics snapshot."""

    def __init__(self, service: DiagnosticsService, workspace_root: str) -> None:
        super().__init__()
        self._service = service
        self._workspace_root = workspace_root
        self.signals = DiagnosticsCollectWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            snapshot = self._service.collect(self._workspace_root)
        except Exception as error:
            snapshot = DiagnosticsSnapshot(
                workspace_root=self._workspace_root,
                generated_at="",
                sections=(),
                versions={},
            )
            # The collection boundary must return an object to the GUI even when
            # an unexpected local error occurs. A synthetic section preserves it.
            from .diagnostics import DiagnosticsSection

            snapshot = DiagnosticsSnapshot(
                workspace_root=self._workspace_root,
                generated_at="",
                sections=(
                    DiagnosticsSection(
                        name="collection",
                        ok=False,
                        operation_id=f"gui-internal:{uuid4()}",
                        project_alias=None,
                        error_code="gui_diagnostics_internal_error",
                        error_message=f"{type(error).__name__}: {error}",
                    ),
                ),
                versions={},
            )
        self.signals.completed.emit(snapshot)


class DiagnosticsExportWorkerSignals(QObject):
    completed = Signal(object)


class DiagnosticsExportWorker(QRunnable):
    """Writes one explicitly requested sanitized diagnostics archive."""

    def __init__(
        self,
        exporter: DiagnosticsExporter,
        snapshot: DiagnosticsSnapshot,
        output_path: str,
        *,
        overwrite: bool,
    ) -> None:
        super().__init__()
        self._exporter = exporter
        self._snapshot = snapshot
        self._output_path = output_path
        self._overwrite = overwrite
        self.signals = DiagnosticsExportWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self._exporter.export(
                self._snapshot,
                self._output_path,
                overwrite=self._overwrite,
            )
            outcome = DiagnosticsExportOutcome.success(result)
        except FileExistsError as error:
            outcome = DiagnosticsExportOutcome.failure("export_exists", str(error))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            outcome = DiagnosticsExportOutcome.failure(
                "diagnostics_export_failed",
                f"{type(error).__name__}: {error}",
            )
        except Exception as error:
            outcome = DiagnosticsExportOutcome.failure(
                "gui_diagnostics_export_internal_error",
                f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(outcome)


import zipfile  # kept at module end to avoid broadening GUI startup work
