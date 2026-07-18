from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
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
from .operations import (
    ControlAction,
    ControlResult,
    ProjectOperationsService,
    ProjectStatusSnapshot,
)
from .state import BootstrapSnapshot
from .workers import (
    BootstrapWorker,
    ControlWorker,
    CurrentOperationWorker,
    StatusWorker,
)


NAVIGATION = (
    ("Dashboard", "Stan runtime i jawne sterowanie BDB"),
    ("Projects", "Skonfigurowane workspace'y"),
    ("Current operation", "Bieżąca read-only projekcja Journalu"),
    ("History", "Zdarzenia i historia"),
    ("Diagnostics", "Diagnostyka i wersje"),
)

ConfirmationProvider = Callable[[ControlAction, str], bool]


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
    dashboard_ready = Signal()

    def __init__(
        self,
        *,
        bootstrap_service: BootstrapService,
        operations_service: ProjectOperationsService,
        workspaces_root: str,
        current_operation_service: CurrentOperationService | None = None,
        auto_load_status: bool = True,
        confirmation_provider: ConfirmationProvider | None = None,
    ) -> None:
        super().__init__()
        self._bootstrap_service = bootstrap_service
        self._operations_service = operations_service
        self._current_operation_service = current_operation_service or CurrentOperationService()
        self._workspaces_root = workspaces_root
        self._auto_load_status = bool(auto_load_status)
        self._confirmation_provider = confirmation_provider
        self._thread_pool = QThreadPool.globalInstance()
        self._bootstrap_worker: BootstrapWorker | None = None
        self._status_worker: StatusWorker | None = None
        self._control_worker: ControlWorker | None = None
        self._current_operation_worker: CurrentOperationWorker | None = None
        self._last_snapshot: BootstrapSnapshot | None = None
        self._last_status: ProjectStatusSnapshot | None = None
        self._last_control_result: ControlResult | None = None
        self._last_current_operation: CurrentOperationSnapshot | None = None
        self._mutation_operations_invoked = 0

        self.setObjectName("BdbControlCenterWindow")
        self.setWindowTitle("BDB Control Center")
        self.resize(1220, 800)
        self.setMinimumSize(980, 650)
        self._build_ui()
        self._apply_style()
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
            "operator_network_listener": (
                snapshot.network_listener if snapshot is not None else None
            ),
            "selected_workspace_root": self._selected_workspace_root(),
            "status_read_completed": status is not None,
            "status_read_ok": status.ok if status is not None else None,
            "status_error_code": status.error_code if status is not None else None,
        }
        report.update(self.dashboard.smoke_report())
        report.update(self.current_operation_view.smoke_report())
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
            "Otwarcie okna pozostaje tylko do odczytu. Start, Stop i re-arm wymagają "
            "osobnego kliknięcia oraz potwierdzenia."
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
        header.setSpacing(12)
        heading_box = QVBoxLayout()
        heading_box.setSpacing(3)
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
                "Lista projektów jest dostępna w selektorze. Pełny kreator i szczegóły zostaną rozwinięte w P11.",
            )
        )
        self.current_operation_view = CurrentOperationWidget()
        self.current_operation_view.refresh_requested.connect(self._start_current_operation_read)
        self.pages.addWidget(self.current_operation_view)
        self.pages.addWidget(
            self._placeholder_page(
                "History",
                "Historia eventów i Journal zostaną rozwinięte w P09.",
            )
        )
        self.pages.addWidget(
            self._placeholder_page(
                "Diagnostics",
                "Eksport diagnostyczny i wersje zostaną rozwinięte w P10.",
            )
        )
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
        cards.setSpacing(12)
        self.operator_card = StatusCard("OPERATOR API", "Ładowanie")
        self.projects_card = StatusCard("PROJEKTY", "—")
        self.safety_card = StatusCard(
            "TRYB STARTOWY",
            "READ-ONLY",
            "Sterowanie wymaga potwierdzenia",
        )
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
        layout.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("PlaceholderPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(28, 26, 28, 26)
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
        self._last_status = None
        self._last_current_operation = None

        self.project_selector.blockSignals(True)
        self.project_selector.clear()
        has_projects = False
        if snapshot.ok:
            if snapshot.projects:
                for project in snapshot.projects:
                    self.project_selector.addItem(project.alias, project.workspace_root)
                self.project_selector.setCurrentIndex(0)
                has_projects = True
            else:
                self.project_selector.addItem("Brak przygotowanych projektów")
            self.operator_card.update_value(
                "GOTOWY",
                f"{snapshot.operator_transport} · Journal {snapshot.journal_access or 'n/a'}",
            )
            self.projects_card.update_value(
                str(len(snapshot.projects)),
                f"Nieprawidłowe wpisy: {len(snapshot.invalid_entries)}",
            )
        else:
            self.project_selector.addItem("Bootstrap niedostępny")
            self.operator_card.update_value("BŁĄD", snapshot.error_code or "unknown")
            self.projects_card.update_value("—", snapshot.workspaces_root)
        self.project_selector.blockSignals(False)
        self._set_global_busy(False)

        if has_projects:
            alias = self.project_selector.currentText()
            workspace_root = self._selected_workspace_root()
            if workspace_root is not None:
                self.dashboard.set_project(alias, workspace_root)
                self.current_operation_view.set_project(alias, workspace_root)
        else:
            self.dashboard.set_project_available(False)
            self.current_operation_view.set_project_available(False)

        if snapshot.ok:
            self.status_line.setText(
                "Lista projektów załadowana bez mutacji. Wybór projektu uruchamia tylko jawne odczyty."
            )
        else:
            self.status_line.setText(
                snapshot.error_message or "Nie udało się załadować bootstrapu."
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
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            self.dashboard.set_project_available(False)
            self.current_operation_view.set_project_available(False)
            return
        alias = self.project_selector.currentText()
        self.dashboard.set_project(alias, workspace_root)
        self.current_operation_view.set_project(alias, workspace_root)
        self._last_status = None
        self._last_current_operation = None
        if self._auto_load_status:
            self._start_status_read()

    @Slot()
    def _start_status_read(self) -> None:
        if self._has_active_task():
            return
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            self.dashboard.set_project_available(False)
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
        if snapshot.ok:
            self.status_line.setText(
                f"Status {snapshot.project_alias or 'projektu'}: {snapshot.overall_status or 'UNKNOWN'}. Odczyt nie zmienił stanu BDB."
            )
        else:
            self.status_line.setText(
                f"Status niedostępny: {snapshot.error_code or 'unknown'} — "
                f"{snapshot.error_message or 'brak szczegółów'}"
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
            self.current_operation_view.set_project_available(False)
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
        if snapshot.ok:
            state = snapshot.operation.state if snapshot.active and snapshot.operation else "none"
            self.status_line.setText(
                f"Bieżąca operacja: {state}. Projekcja Journalu pozostała tylko do odczytu."
            )
        else:
            self.status_line.setText(
                f"Bieżąca operacja niedostępna: {snapshot.error_code or 'unknown'} — "
                f"{snapshot.error_message or 'brak szczegółów'}"
            )
        self.current_operation_finished.emit(snapshot)
        self.dashboard_ready.emit()

    @Slot(str)
    def _request_control(self, action_text: str) -> None:
        if action_text not in {"start", "stop", "rearm"} or self._has_active_task():
            return
        action: ControlAction = action_text  # type: ignore[assignment]
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            return
        if not self._confirm_control(action, workspace_root):
            self.status_line.setText(f"Operacja {action} została anulowana przed wykonaniem.")
            return

        arm_minutes = self.dashboard.arm_minutes
        self._set_global_busy(True, f"Wykonywanie jawnej operacji {action}…")
        worker = ControlWorker(
            self._operations_service,
            action,
            workspace_root,
            arm_minutes=arm_minutes,
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

        if result.ok:
            self.status_line.setText(
                f"Operacja {result.action} zakończona. Pobieram status potwierdzający wynik."
            )
        else:
            self.status_line.setText(
                f"Operacja {result.action} zakończona błędem: {result.error_code or 'unknown'}. "
                "Pobieram status końcowy bez wykonywania kolejnej mutacji."
            )
        self._set_global_busy(False)
        self._start_status_read()

    def _confirm_control(self, action: ControlAction, workspace_root: str) -> bool:
        if self._confirmation_provider is not None:
            return bool(self._confirmation_provider(action, workspace_root))

        titles = {
            "start": "Uruchomić BDB?",
            "stop": "Zatrzymać BDB?",
            "rearm": "Ponownie uzbroić Native Hosta?",
        }
        descriptions = {
            "start": (
                "Uruchomiony zostanie promoter i Bridge, a Native Host zostanie uzbrojony "
                f"na {self.dashboard.arm_minutes} minut."
            ),
            "stop": (
                "Native Host zostanie rozbrojony, a Bridge i promoter zatrzymane kooperacyjnie. "
                "Journal, logi, wyniki, receipts i worktree zostaną zachowane."
            ),
            "rearm": (
                "Bieżący Native Host zostanie jawnie uzbrojony "
                f"na {self.dashboard.arm_minutes} minut."
            ),
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
        self.dashboard.set_busy(busy, message)
        self.current_operation_view.set_busy(busy, message)
        if message:
            self.status_line.setText(message)

    @Slot(int)
    def _select_page(self, index: int) -> None:
        if index < 0 or index >= len(NAVIGATION):
            return
        self.pages.setCurrentIndex(index)
        self.page_title.setText(NAVIGATION[index][0])
        self.page_subtitle.setText(NAVIGATION[index][1])

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, #AppShell, #Content { background: #f4f6f8; color: #172033; }
            #Sidebar { background: #111827; color: #f8fafc; }
            #BrandMark { color: #93c5fd; font-size: 12px; font-weight: 800; letter-spacing: 3px; }
            #BrandTitle { color: #ffffff; font-size: 23px; font-weight: 700; }
            #BrandSubtitle { color: #94a3b8; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
            #Navigation { background: transparent; color: #cbd5e1; outline: 0; }
            #Navigation::item { padding: 12px 13px; border-radius: 8px; }
            #Navigation::item:hover { background: #1f2937; color: #ffffff; }
            #Navigation::item:selected { background: #2563eb; color: #ffffff; }
            #SafetyPanel { background: #172033; border: 1px solid #29364b; border-radius: 10px; }
            #SafetyTitle { color: #86efac; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
            #SafetyText { color: #aebbd0; font-size: 11px; }
            #PageTitle { color: #111827; font-size: 25px; font-weight: 700; }
            #PageSubtitle { color: #64748b; font-size: 12px; }
            #ProjectSelector, #RefreshButton { min-height: 34px; border-radius: 7px; }
            #ProjectSelector { background: #ffffff; border: 1px solid #d7dde6; padding: 0 10px; }
            #RefreshButton { background: #ffffff; border: 1px solid #cbd5e1; padding: 0 14px; color: #1e293b; }
            #RefreshButton:hover { background: #eef2f7; }
            #RefreshButton:disabled { color: #94a3b8; background: #eef2f7; }
            #StatusCard, #HeroPanel, #RuntimeCard, #ControlPanel, #PlaceholderPanel,
            #OperationHeroPanel, #OperationDetailsPanel {
                background: #ffffff; border: 1px solid #dfe5ec; border-radius: 12px;
            }
            #StatusCardTitle, #RuntimeCardTitle, #ControlTitle, #OperationSectionTitle {
                color: #64748b; font-size: 10px; font-weight: 700; letter-spacing: 1px;
            }
            #StatusCardValue, #RuntimeCardValue { color: #111827; font-size: 19px; font-weight: 700; }
            #StatusCardDetail, #RuntimeCardDetail, #ControlDescription, #HeroText, #PlaceholderText,
            #OperationFeedback, #OperationFieldLabel {
                color: #64748b; font-size: 11px;
            }
            #HeroTitle, #PlaceholderTitle { color: #172033; font-size: 18px; font-weight: 700; }
            #OverallStatus, #OperationState {
                color: #1d4ed8; background: #eff6ff; border: 1px solid #bfdbfe;
                border-radius: 7px; padding: 6px 10px; font-size: 11px; font-weight: 800;
            }
            #OperationFieldValue { color: #1e293b; font-size: 11px; font-family: Consolas; }
            #RefreshStatusButton, #StartButton, #StopButton, #RearmButton, #RefreshOperationButton {
                min-height: 34px; border-radius: 7px; padding: 0 14px; font-weight: 600;
            }
            #RefreshStatusButton, #RefreshOperationButton { background: #ffffff; border: 1px solid #cbd5e1; color: #1e293b; }
            #StartButton { background: #166534; border: 1px solid #14532d; color: #ffffff; }
            #StopButton { background: #991b1b; border: 1px solid #7f1d1d; color: #ffffff; }
            #RearmButton { background: #1d4ed8; border: 1px solid #1e40af; color: #ffffff; }
            #RefreshStatusButton:disabled, #StartButton:disabled, #StopButton:disabled,
            #RearmButton:disabled, #RefreshOperationButton:disabled {
                background: #e5e7eb; border-color: #d1d5db; color: #9ca3af;
            }
            #ArmMinutesSpin { min-height: 32px; min-width: 82px; }
            #ArmMinutesLabel { color: #475569; font-size: 11px; }
            #ControlFeedback { color: #475569; font-size: 11px; }
            #ControlFeedback[busy="true"] { color: #1d4ed8; }
            #StatusLine { color: #64748b; font-size: 11px; }
            """
        )
