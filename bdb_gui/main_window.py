from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .bootstrap import BootstrapService
from .current_operation import CurrentOperationService, CurrentOperationSnapshot
from .current_operation_view import CurrentOperationWidget
from .dashboard import DashboardWidget
from .diagnostics import (
    DiagnosticsExporter,
    DiagnosticsExportResult,
    DiagnosticsService,
    DiagnosticsSnapshot,
)
from .diagnostics_tasks import DiagnosticsExportOutcome
from .diagnostics_view import DiagnosticsWidget
from .history import HistoryService, HistorySnapshot
from .history_view import HistoryWidget
from .operations import ControlAction, ControlResult, ProjectOperationsService, ProjectStatusSnapshot
from .state import BootstrapSnapshot
from .style import CONTROL_CENTER_STYLESHEET
from .workers import (
    BootstrapWorker,
    ControlWorker,
    CurrentOperationWorker,
    DiagnosticsCollectWorker,
    DiagnosticsExportWorker,
    HistoryWorker,
    StatusWorker,
)


NAVIGATION = (
    ("Dashboard", "Stan runtime i jawne sterowanie BDB"),
    ("Projects", "Skonfigurowane workspace'y"),
    ("Current operation", "Bieżąca read-only projekcja Journalu"),
    ("History", "Filtrowana i stronicowana historia Journalu"),
    ("Diagnostics", "Bounded diagnostyka i jawny sanitizowany eksport"),
)

ConfirmationProvider = Callable[[ControlAction, str], bool]
ExportPathProvider = Callable[[str], tuple[str | None, bool]]


class StatusCard(QFrame):
    def __init__(self, title: str, value: str = "—", detail: str = "") -> None:
        super().__init__()
        self.setObjectName("StatusCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(5)
        title_label = QLabel(title)
        title_label.setObjectName("StatusCardTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatusCardValue")
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("StatusCardDetail")
        self.detail_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)

    def update_value(self, value: str, detail: str = "") -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)


class ControlCenterWindow(QMainWindow):
    bootstrap_finished = Signal(object)
    status_finished = Signal(object)
    control_finished = Signal(object)
    current_operation_finished = Signal(object)
    history_finished = Signal(object, bool)
    diagnostics_finished = Signal(object)
    diagnostics_export_finished = Signal(object)
    dashboard_ready = Signal()

    def __init__(
        self,
        *,
        bootstrap_service: BootstrapService,
        operations_service: ProjectOperationsService,
        workspaces_root: str,
        current_operation_service: CurrentOperationService | None = None,
        history_service: HistoryService | None = None,
        diagnostics_service: DiagnosticsService | None = None,
        diagnostics_exporter: DiagnosticsExporter | None = None,
        auto_load_status: bool = True,
        confirmation_provider: ConfirmationProvider | None = None,
        export_path_provider: ExportPathProvider | None = None,
    ) -> None:
        super().__init__()
        self._bootstrap_service = bootstrap_service
        self._operations_service = operations_service
        self._current_operation_service = current_operation_service or CurrentOperationService()
        self._history_service = history_service or HistoryService()
        self._diagnostics_service = diagnostics_service or DiagnosticsService()
        self._diagnostics_exporter = diagnostics_exporter or DiagnosticsExporter()
        self._workspaces_root = workspaces_root
        self._auto_load_status = bool(auto_load_status)
        self._confirmation_provider = confirmation_provider
        self._export_path_provider = export_path_provider
        self._thread_pool = QThreadPool.globalInstance()
        self._bootstrap_worker: BootstrapWorker | None = None
        self._status_worker: StatusWorker | None = None
        self._control_worker: ControlWorker | None = None
        self._current_operation_worker: CurrentOperationWorker | None = None
        self._history_worker: HistoryWorker | None = None
        self._diagnostics_worker: DiagnosticsCollectWorker | None = None
        self._diagnostics_export_worker: DiagnosticsExportWorker | None = None
        self._last_snapshot: BootstrapSnapshot | None = None
        self._last_status: ProjectStatusSnapshot | None = None
        self._last_control_result: ControlResult | None = None
        self._last_current_operation: CurrentOperationSnapshot | None = None
        self._last_history: HistorySnapshot | None = None
        self._last_diagnostics: DiagnosticsSnapshot | None = None
        self._last_diagnostics_export: DiagnosticsExportResult | None = None
        self._mutation_operations_invoked = 0

        self.setObjectName("BdbControlCenterWindow")
        self.setWindowTitle("BDB Control Center")
        self.resize(1260, 820)
        self.setMinimumSize(1000, 660)
        self._build_ui()
        self.setStyleSheet(CONTROL_CENTER_STYLESHEET)
        self._show_loading_state()

    @property
    def last_snapshot(self) -> BootstrapSnapshot | None:
        return self._last_snapshot

    @property
    def last_status(self) -> ProjectStatusSnapshot | None:
        return self._last_status

    @property
    def last_control_result(self) -> ControlResult | None:
        return self._last_control_result

    @property
    def last_current_operation(self) -> CurrentOperationSnapshot | None:
        return self._last_current_operation

    @property
    def last_history(self) -> HistorySnapshot | None:
        return self._last_history

    @property
    def last_diagnostics(self) -> DiagnosticsSnapshot | None:
        return self._last_diagnostics

    @property
    def last_diagnostics_export(self) -> DiagnosticsExportResult | None:
        return self._last_diagnostics_export

    def start_bootstrap(self) -> None:
        if self._has_active_task():
            return
        self._show_loading_state()
        worker = BootstrapWorker(self._bootstrap_service, self._workspaces_root)
        worker.signals.completed.connect(self._apply_bootstrap_snapshot)
        self._bootstrap_worker = worker
        self._thread_pool.start(worker)

    def smoke_report(self) -> dict[str, Any]:
        snapshot = self._last_snapshot
        status = self._last_status
        report = {
            "schema": "bdb-control-center-smoke-v1",
            "window_object_name": self.objectName(),
            "window_constructed": self.objectName() == "BdbControlCenterWindow",
            "read_only_startup": True,
            "navigation": [label for label, _ in NAVIGATION],
            "page_count": self.pages.count(),
            "project_count": len(snapshot.projects) if snapshot is not None else 0,
            "bootstrap_completed": snapshot is not None,
            "bootstrap_ok": snapshot.ok if snapshot is not None else False,
            "bootstrap_error_code": snapshot.error_code if snapshot is not None else None,
            "mutation_operations_invoked": self._mutation_operations_invoked,
            "operator_network_listener": snapshot.network_listener if snapshot is not None else None,
            "selected_workspace_root": self._selected_workspace_root(),
            "status_read_completed": status is not None,
            "status_read_ok": status.ok if status is not None else None,
            "status_error_code": status.error_code if status is not None else None,
        }
        report.update(self.dashboard.smoke_report())
        report.update(self.current_operation_view.smoke_report())
        report.update(self.history_view.smoke_report())
        report.update(self.diagnostics_view.smoke_report())
        return report

    def _build_ui(self) -> None:
        shell = QWidget(self)
        shell.setObjectName("AppShell")
        root = QHBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_content(), 1)
        self.setCentralWidget(shell)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(245)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 20)
        layout.setSpacing(14)
        brand = QLabel("BDB")
        brand.setObjectName("BrandMark")
        title = QLabel("Control Center")
        title.setObjectName("BrandTitle")
        subtitle = QLabel("LOCAL OPERATOR PANEL")
        subtitle.setObjectName("BrandSubtitle")
        layout.addWidget(brand)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(18)
        self.navigation = QListWidget()
        self.navigation.setObjectName("Navigation")
        self.navigation.setFrameShape(QFrame.Shape.NoFrame)
        self.navigation.setSpacing(4)
        for label, tooltip in NAVIGATION:
            item = QListWidgetItem(label)
            item.setToolTip(tooltip)
            item.setData(Qt.ItemDataRole.UserRole, label)
            self.navigation.addItem(item)
        self.navigation.currentRowChanged.connect(self._select_page)
        layout.addWidget(self.navigation, 1)
        safety = QFrame()
        safety.setObjectName("SafetyPanel")
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(12, 11, 12, 11)
        safety_layout.setSpacing(4)
        safety_title = QLabel("EXPLICIT MUTATIONS")
        safety_title.setObjectName("SafetyTitle")
        safety_text = QLabel(
            "Otwarcie okna pozostaje tylko do odczytu. Sterowanie i eksport wymagają "
            "osobnych działań użytkownika."
        )
        safety_text.setObjectName("SafetyText")
        safety_text.setWordWrap(True)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)
        layout.addWidget(safety)
        return sidebar

    def _build_content(self) -> QWidget:
        content = QWidget()
        content.setObjectName("Content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 22, 28, 26)
        layout.setSpacing(18)
        header = QHBoxLayout()
        heading_box = QVBoxLayout()
        self.page_title = QLabel("Dashboard")
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel(NAVIGATION[0][1])
        self.page_subtitle.setObjectName("PageSubtitle")
        heading_box.addWidget(self.page_title)
        heading_box.addWidget(self.page_subtitle)
        header.addLayout(heading_box)
        header.addStretch(1)
        self.project_selector = QComboBox()
        self.project_selector.setObjectName("ProjectSelector")
        self.project_selector.setMinimumWidth(260)
        self.project_selector.setEnabled(False)
        self.project_selector.addItem("Ładowanie projektów…")
        self.project_selector.currentIndexChanged.connect(self._project_selected)
        header.addWidget(self.project_selector)
        self.refresh_button = QPushButton("Odśwież projekty")
        self.refresh_button.setObjectName("RefreshButton")
        self.refresh_button.clicked.connect(self.start_bootstrap)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.pages = QStackedWidget()
        self.pages.setObjectName("Pages")
        self.pages.addWidget(self._build_dashboard_page())
        self.pages.addWidget(
            self._placeholder_page(
                "Projects",
                "Lista projektów jest dostępna w selektorze. Kreator zostanie rozwinięty w P11.",
            )
        )
        self.current_operation_view = CurrentOperationWidget()
        self.current_operation_view.refresh_requested.connect(self._start_current_operation_read)
        self.pages.addWidget(self.current_operation_view)
        self.history_view = HistoryWidget()
        self.history_view.query_requested.connect(self._start_history_read)
        self.pages.addWidget(self.history_view)
        self.diagnostics_view = DiagnosticsWidget()
        self.diagnostics_view.collect_requested.connect(self._start_diagnostics_collect)
        self.diagnostics_view.export_requested.connect(self._request_diagnostics_export)
        self.pages.addWidget(self.diagnostics_view)
        layout.addWidget(self.pages, 1)
        self.status_line = QLabel("Inicjalizacja warstwy tylko do odczytu…")
        self.status_line.setObjectName("StatusLine")
        self.status_line.setWordWrap(True)
        layout.addWidget(self.status_line)
        self.navigation.setCurrentRow(0)
        return content

    def _build_dashboard_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("DashboardContainer")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        cards = QHBoxLayout()
        self.operator_card = StatusCard("OPERATOR API", "Ładowanie")
        self.projects_card = StatusCard("PROJEKTY", "—")
        self.safety_card = StatusCard("TRYB STARTOWY", "READ-ONLY", "Sterowanie wymaga potwierdzenia")
        for card in (self.operator_card, self.projects_card, self.safety_card):
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            cards.addWidget(card)
        layout.addLayout(cards)
        self.dashboard = DashboardWidget()
        self.dashboard.refresh_status_requested.connect(self._start_status_read)
        self.dashboard.control_requested.connect(self._request_control)
        layout.addWidget(self.dashboard, 1)
        return page

    def _placeholder_page(self, title: str, description: str) -> QWidget:
        page = QWidget()
        page.setObjectName(title.replace(" ", "") + "Page")
        layout = QVBoxLayout(page)
        panel = QFrame()
        panel.setObjectName("PlaceholderPanel")
        panel_layout = QVBoxLayout(panel)
        heading = QLabel(title)
        heading.setObjectName("PlaceholderTitle")
        text = QLabel(description)
        text.setObjectName("PlaceholderText")
        text.setWordWrap(True)
        panel_layout.addWidget(heading)
        panel_layout.addWidget(text)
        panel_layout.addStretch(1)
        layout.addWidget(panel)
        return page

    def _show_loading_state(self) -> None:
        self._set_global_busy(True, "Pobieranie capabilities i listy projektów…")
        self.operator_card.update_value("Ładowanie", "Operator API in-process")
        self.projects_card.update_value("—", self._workspaces_root)

    @Slot(object)
    def _apply_bootstrap_snapshot(self, snapshot: BootstrapSnapshot) -> None:
        self._last_snapshot = snapshot
        self._bootstrap_worker = None
        self._reset_project_reads()
        self.project_selector.blockSignals(True)
        self.project_selector.clear()
        has_projects = False
        if snapshot.ok and snapshot.projects:
            for project in snapshot.projects:
                self.project_selector.addItem(project.alias, project.workspace_root)
            self.project_selector.setCurrentIndex(0)
            has_projects = True
        elif snapshot.ok:
            self.project_selector.addItem("Brak przygotowanych projektów")
        else:
            self.project_selector.addItem("Bootstrap niedostępny")
        self.project_selector.blockSignals(False)
        self._set_global_busy(False)

        if snapshot.ok:
            self.operator_card.update_value(
                "GOTOWY", f"{snapshot.operator_transport} · Journal {snapshot.journal_access or 'n/a'}"
            )
            self.projects_card.update_value(
                str(len(snapshot.projects)), f"Nieprawidłowe wpisy: {len(snapshot.invalid_entries)}"
            )
        else:
            self.operator_card.update_value("BŁĄD", snapshot.error_code or "unknown")
            self.projects_card.update_value("—", snapshot.workspaces_root)

        if has_projects:
            self._apply_selected_project_to_views()
        else:
            self._set_project_available(False)
        self.status_line.setText(
            "Lista projektów załadowana bez mutacji."
            if snapshot.ok
            else snapshot.error_message or "Nie udało się załadować bootstrapu."
        )
        self.bootstrap_finished.emit(snapshot)
        if has_projects and self._auto_load_status:
            self._start_status_read()
        else:
            self.dashboard_ready.emit()

    @Slot(int)
    def _project_selected(self, index: int) -> None:
        if index < 0 or self._has_active_task():
            return
        if self._selected_workspace_root() is None:
            self._set_project_available(False)
            return
        self._reset_project_reads()
        self._apply_selected_project_to_views()
        if self._auto_load_status:
            self._start_status_read()

    def _apply_selected_project_to_views(self) -> None:
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            self._set_project_available(False)
            return
        alias = self.project_selector.currentText()
        for view in (
            self.dashboard,
            self.current_operation_view,
            self.history_view,
            self.diagnostics_view,
        ):
            view.set_project(alias, workspace_root)

    def _set_project_available(self, available: bool) -> None:
        self.dashboard.set_project_available(available)
        self.current_operation_view.set_project_available(available)
        self.history_view.set_project_available(available)
        self.diagnostics_view.set_project_available(available)

    def _reset_project_reads(self) -> None:
        self._last_status = None
        self._last_control_result = None
        self._last_current_operation = None
        self._last_history = None
        self._last_diagnostics = None
        self._last_diagnostics_export = None

    @Slot()
    def _start_status_read(self) -> None:
        if self._has_active_task():
            return
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            return
        self._set_global_busy(True, "Pobieranie statusu projektu tylko do odczytu…")
        worker = StatusWorker(self._operations_service, workspace_root)
        worker.signals.completed.connect(self._apply_status_snapshot)
        self._status_worker = worker
        self._thread_pool.start(worker)

    @Slot(object)
    def _apply_status_snapshot(self, snapshot: ProjectStatusSnapshot) -> None:
        self._status_worker = None
        self._last_status = snapshot
        self._set_global_busy(False)
        self.dashboard.apply_status(snapshot)
        self.status_line.setText(
            f"Status: {snapshot.overall_status or 'UNKNOWN'}. Odczyt nie zmienił BDB."
            if snapshot.ok
            else f"Status niedostępny: {snapshot.error_code or 'unknown'} — {snapshot.error_message or 'brak szczegółów'}"
        )
        self.status_finished.emit(snapshot)
        if self._auto_load_status:
            self._start_current_operation_read()
        else:
            self.dashboard_ready.emit()

    @Slot()
    def _start_current_operation_read(self) -> None:
        if self._has_active_task():
            return
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            return
        self._set_global_busy(True, "Odczytywanie bieżącej operacji z Journalu…")
        worker = CurrentOperationWorker(self._current_operation_service, workspace_root)
        worker.signals.completed.connect(self._apply_current_operation_snapshot)
        self._current_operation_worker = worker
        self._thread_pool.start(worker)

    @Slot(object)
    def _apply_current_operation_snapshot(self, snapshot: CurrentOperationSnapshot) -> None:
        self._current_operation_worker = None
        self._last_current_operation = snapshot
        self._set_global_busy(False)
        self.current_operation_view.apply_snapshot(snapshot)
        state = snapshot.operation.state if snapshot.ok and snapshot.active and snapshot.operation else "none"
        self.status_line.setText(
            f"Bieżąca operacja: {state}. Projekcja pozostała read-only."
            if snapshot.ok
            else f"Bieżąca operacja niedostępna: {snapshot.error_code or 'unknown'}"
        )
        self.current_operation_finished.emit(snapshot)
        self.dashboard_ready.emit()

    @Slot(object)
    def _start_history_read(self, query: object) -> None:
        if self._has_active_task() or not isinstance(query, dict):
            return
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            return
        after_event_id = query.get("after_event_id", 0)
        limit = query.get("limit", 100)
        session_id = query.get("session_id")
        command_id = query.get("command_id")
        append = bool(query.get("append", False))
        if not isinstance(after_event_id, int) or isinstance(after_event_id, bool):
            return
        if not isinstance(limit, int) or isinstance(limit, bool):
            return
        if session_id is not None and not isinstance(session_id, str):
            return
        if command_id is not None and not isinstance(command_id, str):
            return
        self._set_global_busy(True, "Odczytywanie ograniczonej strony historii…")
        worker = HistoryWorker(
            self._history_service,
            workspace_root,
            after_event_id=after_event_id,
            limit=limit,
            session_id=session_id,
            command_id=command_id,
            append=append,
        )
        worker.signals.completed.connect(self._apply_history_snapshot)
        self._history_worker = worker
        self._thread_pool.start(worker)

    @Slot(object, bool)
    def _apply_history_snapshot(self, snapshot: HistorySnapshot, append: bool) -> None:
        self._history_worker = None
        self._last_history = snapshot
        self._set_global_busy(False)
        self.history_view.apply_snapshot(snapshot, append=append)
        self.status_line.setText(
            f"Historia: {len(snapshot.events)} zdarzeń z bieżącej strony."
            if snapshot.ok
            else f"Historia niedostępna: {snapshot.error_code or 'unknown'}"
        )
        self.history_finished.emit(snapshot, append)

    @Slot()
    def _start_diagnostics_collect(self) -> None:
        if self._has_active_task():
            return
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            return
        self._set_global_busy(True, "Zbieranie bounded diagnostyki tylko do odczytu…")
        worker = DiagnosticsCollectWorker(self._diagnostics_service, workspace_root)
        worker.signals.completed.connect(self._apply_diagnostics_snapshot)
        self._diagnostics_worker = worker
        self._thread_pool.start(worker)

    @Slot(object)
    def _apply_diagnostics_snapshot(self, snapshot: DiagnosticsSnapshot) -> None:
        self._diagnostics_worker = None
        self._last_diagnostics = snapshot
        self._last_diagnostics_export = None
        self._set_global_busy(False)
        self.diagnostics_view.apply_snapshot(snapshot)
        self.status_line.setText(
            f"Diagnostyka zebrana: {'kompletna' if snapshot.complete else 'częściowa'}; eksport nie został wykonany."
        )
        self.diagnostics_finished.emit(snapshot)

    @Slot()
    def _request_diagnostics_export(self) -> None:
        if self._has_active_task() or self._last_diagnostics is None:
            return
        suggested = self._diagnostics_filename()
        if self._export_path_provider is not None:
            output_path, overwrite = self._export_path_provider(suggested)
        else:
            output_path, overwrite = self._choose_diagnostics_export_path(suggested)
        if not output_path:
            self.status_line.setText("Eksport diagnostyczny anulowany przed zapisem.")
            return
        self._set_global_busy(True, "Zapisywanie sanitizowanego pakietu diagnostycznego…")
        worker = DiagnosticsExportWorker(
            self._diagnostics_exporter,
            self._last_diagnostics,
            output_path,
            overwrite=overwrite,
        )
        worker.signals.completed.connect(self._apply_diagnostics_export_outcome)
        self._diagnostics_export_worker = worker
        self._thread_pool.start(worker)

    @Slot(object)
    def _apply_diagnostics_export_outcome(self, outcome: DiagnosticsExportOutcome) -> None:
        self._diagnostics_export_worker = None
        self._set_global_busy(False)
        if outcome.ok and outcome.result is not None:
            self._last_diagnostics_export = outcome.result
            self.diagnostics_view.apply_export_result(outcome.result)
            self.status_line.setText(f"Pakiet diagnostyczny zapisany: {outcome.result.output_path}")
        else:
            self.diagnostics_view.apply_export_error(
                outcome.error_code or "diagnostics_export_failed",
                outcome.error_message or "brak szczegółów",
            )
            self.status_line.setText("Eksport diagnostyczny nie został zakończony.")
        self.diagnostics_export_finished.emit(outcome)

    def _choose_diagnostics_export_path(self, suggested: str) -> tuple[str | None, bool]:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Eksportuj sanitizowaną diagnostykę BDB",
            str(Path.home() / suggested),
            "ZIP archive (*.zip)",
        )
        if not selected:
            return None, False
        path = Path(selected)
        if path.suffix.lower() != ".zip":
            path = path.with_suffix(".zip")
        overwrite = False
        if path.exists():
            answer = QMessageBox.question(
                self,
                "Nadpisać istniejący plik?",
                f"Plik już istnieje:\n{path}\n\nNadpisać go atomowo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None, False
            overwrite = True
        return str(path), overwrite

    def _diagnostics_filename(self) -> str:
        alias = self.project_selector.currentText().strip() or "project"
        safe_alias = "".join(char if char.isalnum() or char in "-_" else "_" for char in alias)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"bdb-diagnostics-{safe_alias}-{stamp}.zip"

    @Slot(str)
    def _request_control(self, action_text: str) -> None:
        if action_text not in {"start", "stop", "rearm"} or self._has_active_task():
            return
        action: ControlAction = action_text  # type: ignore[assignment]
        workspace_root = self._selected_workspace_root()
        if workspace_root is None or not self._confirm_control(action, workspace_root):
            if workspace_root is not None:
                self.status_line.setText(f"Operacja {action} została anulowana przed wykonaniem.")
            return
        self._set_global_busy(True, f"Wykonywanie jawnej operacji {action}…")
        worker = ControlWorker(
            self._operations_service,
            action,
            workspace_root,
            arm_minutes=self.dashboard.arm_minutes,
        )
        worker.signals.completed.connect(self._apply_control_result)
        self._control_worker = worker
        self._thread_pool.start(worker)

    @Slot(object)
    def _apply_control_result(self, result: ControlResult) -> None:
        self._control_worker = None
        self._last_control_result = result
        self._mutation_operations_invoked += result.mutation_operations_invoked
        self.dashboard.apply_control_result(result)
        self.control_finished.emit(result)
        self._set_global_busy(False)
        self._start_status_read()

    def _confirm_control(self, action: ControlAction, workspace_root: str) -> bool:
        if self._confirmation_provider is not None:
            return bool(self._confirmation_provider(action, workspace_root))
        titles = {"start": "Uruchomić BDB?", "stop": "Zatrzymać BDB?", "rearm": "Uzbroić hosta?"}
        descriptions = {
            "start": f"Bridge i promoter zostaną uruchomione, host uzbrojony na {self.dashboard.arm_minutes} minut.",
            "stop": "Bridge i promoter zostaną zatrzymane kooperacyjnie; Journal i worktree pozostaną.",
            "rearm": f"Native Host zostanie uzbrojony na {self.dashboard.arm_minutes} minut.",
        }
        answer = QMessageBox.question(
            self,
            titles[action],
            f"Projekt: {self.project_selector.currentText()}\n\n{descriptions[action]}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _selected_workspace_root(self) -> str | None:
        value = self.project_selector.currentData()
        return value if isinstance(value, str) and value else None

    def _has_active_task(self) -> bool:
        return any(
            worker is not None
            for worker in (
                self._bootstrap_worker,
                self._status_worker,
                self._control_worker,
                self._current_operation_worker,
                self._history_worker,
                self._diagnostics_worker,
                self._diagnostics_export_worker,
            )
        )

    def _set_global_busy(self, busy: bool, message: str = "") -> None:
        self.refresh_button.setEnabled(not busy)
        self.project_selector.setEnabled(
            not busy
            and self._last_snapshot is not None
            and self._last_snapshot.ok
            and bool(self._last_snapshot.projects)
        )
        for view in (
            self.dashboard,
            self.current_operation_view,
            self.history_view,
            self.diagnostics_view,
        ):
            view.set_busy(busy, message)
        if message:
            self.status_line.setText(message)

    @Slot(int)
    def _select_page(self, index: int) -> None:
        if 0 <= index < len(NAVIGATION):
            self.pages.setCurrentIndex(index)
            self.page_title.setText(NAVIGATION[index][0])
            self.page_subtitle.setText(NAVIGATION[index][1])

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        super().closeEvent(event)
