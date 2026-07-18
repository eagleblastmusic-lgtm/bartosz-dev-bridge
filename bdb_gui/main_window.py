from __future__ import annotations

from typing import Any

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
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .bootstrap import BootstrapService
from .state import BootstrapSnapshot
from .workers import BootstrapWorker


NAVIGATION = (
    ("Dashboard", "Ogólny stan Control Center"),
    ("Projects", "Skonfigurowane workspace'y"),
    ("Current operation", "Bieżąca operacja BDB"),
    ("History", "Zdarzenia i historia"),
    ("Diagnostics", "Diagnostyka i wersje"),
)


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

    def __init__(
        self,
        *,
        bootstrap_service: BootstrapService,
        workspaces_root: str,
    ) -> None:
        super().__init__()
        self._bootstrap_service = bootstrap_service
        self._workspaces_root = workspaces_root
        self._thread_pool = QThreadPool.globalInstance()
        self._worker: BootstrapWorker | None = None
        self._last_snapshot: BootstrapSnapshot | None = None
        self._closed_explicitly = False

        self.setObjectName("BdbControlCenterWindow")
        self.setWindowTitle("BDB Control Center")
        self.resize(1160, 760)
        self.setMinimumSize(920, 620)
        self._build_ui()
        self._apply_style()
        self._show_loading_state()

    @property
    def last_snapshot(self) -> BootstrapSnapshot | None:
        return self._last_snapshot

    def start_bootstrap(self) -> None:
        if self._worker is not None:
            return
        self._show_loading_state()
        worker = BootstrapWorker(self._bootstrap_service, self._workspaces_root)
        worker.signals.completed.connect(self._apply_bootstrap_snapshot)
        self._worker = worker
        self._thread_pool.start(worker)

    def smoke_report(self) -> dict[str, Any]:
        snapshot = self._last_snapshot
        return {
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
            "mutation_operations_invoked": (
                snapshot.mutation_operations_invoked if snapshot is not None else 0
            ),
            "operator_network_listener": (
                snapshot.network_listener if snapshot is not None else None
            ),
        }

    def _build_ui(self) -> None:
        shell = QWidget(self)
        shell.setObjectName("AppShell")
        root = QHBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = self._build_sidebar()
        content = self._build_content()
        root.addWidget(sidebar)
        root.addWidget(content, 1)
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
        safety_title = QLabel("READ-ONLY STARTUP")
        safety_title.setObjectName("SafetyTitle")
        safety_text = QLabel("Otwarcie okna nie uruchamia Bridge'a, nie uzbraja hosta i nie modyfikuje repozytoriów.")
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
        self.project_selector.setMinimumWidth(240)
        self.project_selector.setEnabled(False)
        self.project_selector.addItem("Ładowanie projektów…")
        header.addWidget(self.project_selector)

        self.refresh_button = QPushButton("Odśwież odczyt")
        self.refresh_button.setObjectName("RefreshButton")
        self.refresh_button.clicked.connect(self.start_bootstrap)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.pages = QStackedWidget()
        self.pages.setObjectName("Pages")
        self.pages.addWidget(self._build_dashboard_page())
        self.pages.addWidget(self._placeholder_page("Projects", "Lista i szczegóły projektów zostaną rozwinięte w P07/P11."))
        self.pages.addWidget(self._placeholder_page("Current operation", "Widok bieżącej operacji zostanie podłączony w P08."))
        self.pages.addWidget(self._placeholder_page("History", "Historia eventów i Journal zostaną rozwinięte w P09."))
        self.pages.addWidget(self._placeholder_page("Diagnostics", "Eksport diagnostyczny i wersje zostaną rozwinięte w P10."))
        layout.addWidget(self.pages, 1)

        self.status_line = QLabel("Inicjalizacja warstwy tylko do odczytu…")
        self.status_line.setObjectName("StatusLine")
        self.status_line.setWordWrap(True)
        layout.addWidget(self.status_line)
        self.navigation.setCurrentRow(0)
        return content

    def _build_dashboard_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("DashboardPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        intro = QFrame()
        intro.setObjectName("HeroPanel")
        intro_layout = QVBoxLayout(intro)
        intro_layout.setContentsMargins(24, 22, 24, 22)
        intro_layout.setSpacing(7)
        intro_title = QLabel("Bezpieczny podgląd lokalnego środowiska BDB")
        intro_title.setObjectName("HeroTitle")
        intro_text = QLabel(
            "P06 ładuje wyłącznie capabilities i listę przygotowanych workspace'ów. "
            "Sterowanie procesami pojawi się dopiero w P07 jako jawne akcje użytkownika."
        )
        intro_text.setObjectName("HeroText")
        intro_text.setWordWrap(True)
        intro_layout.addWidget(intro_title)
        intro_layout.addWidget(intro_text)
        layout.addWidget(intro)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.operator_card = StatusCard("Operator API", "Ładowanie")
        self.projects_card = StatusCard("Projekty", "—")
        self.safety_card = StatusCard("Tryb startowy", "READ-ONLY", "Brak ukrytych mutacji")
        for card in (self.operator_card, self.projects_card, self.safety_card):
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            cards.addWidget(card)
        layout.addLayout(cards)
        layout.addStretch(1)
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
        self.refresh_button.setEnabled(False)
        self.status_line.setText("Pobieranie capabilities i listy projektów w wątku roboczym…")
        self.operator_card.update_value("Ładowanie", "Operator API in-process")
        self.projects_card.update_value("—", self._workspaces_root)

    @Slot(object)
    def _apply_bootstrap_snapshot(self, snapshot: BootstrapSnapshot) -> None:
        self._last_snapshot = snapshot
        self._worker = None
        self.refresh_button.setEnabled(True)
        self.project_selector.clear()

        if snapshot.ok:
            if snapshot.projects:
                for project in snapshot.projects:
                    self.project_selector.addItem(project.alias, project.workspace_root)
                self.project_selector.setEnabled(True)
            else:
                self.project_selector.addItem("Brak przygotowanych projektów")
                self.project_selector.setEnabled(False)
            self.operator_card.update_value(
                "GOTOWY",
                f"{snapshot.operator_transport} · Journal {snapshot.journal_access or 'n/a'}",
            )
            self.projects_card.update_value(
                str(len(snapshot.projects)),
                f"Nieprawidłowe wpisy: {len(snapshot.invalid_entries)}",
            )
            self.status_line.setText(
                "Bootstrap zakończony. Okno pozostaje tylko do odczytu; nie wykonano żadnej mutacji."
            )
        else:
            self.project_selector.addItem("Bootstrap niedostępny")
            self.project_selector.setEnabled(False)
            self.operator_card.update_value("BŁĄD", snapshot.error_code or "unknown")
            self.projects_card.update_value("—", snapshot.workspaces_root)
            self.status_line.setText(snapshot.error_message or "Nie udało się załadować bootstrapu.")

        self.bootstrap_finished.emit(snapshot)

    @Slot(int)
    def _select_page(self, index: int) -> None:
        if index < 0 or index >= len(NAVIGATION):
            return
        self.pages.setCurrentIndex(index)
        self.page_title.setText(NAVIGATION[index][0])
        self.page_subtitle.setText(NAVIGATION[index][1])

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._closed_explicitly = True
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
            #HeroPanel, #StatusCard, #PlaceholderPanel { background: #ffffff; border: 1px solid #dfe5ec; border-radius: 12px; }
            #HeroTitle { color: #172033; font-size: 18px; font-weight: 700; }
            #HeroText, #PlaceholderText { color: #64748b; font-size: 12px; }
            #StatusCardTitle { color: #64748b; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
            #StatusCardValue { color: #111827; font-size: 20px; font-weight: 700; }
            #StatusCardDetail { color: #64748b; font-size: 11px; }
            #PlaceholderTitle { color: #172033; font-size: 20px; font-weight: 700; }
            #StatusLine { color: #64748b; font-size: 11px; }
            """
        )
