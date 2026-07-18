from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .history import GuiEvent, HistorySnapshot


class HistoryWidget(QWidget):
    query_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("HistoryPage")
        self._project_available = False
        self._busy = False
        self._events: list[GuiEvent] = []
        self._next_after_event_id = 0
        self._has_more = False
        self._last_snapshot: HistorySnapshot | None = None
        self._build_ui()
        self.set_project_available(False)

    @property
    def last_snapshot(self) -> HistorySnapshot | None:
        return self._last_snapshot

    @property
    def event_count(self) -> int:
        return len(self._events)

    def set_project_available(self, available: bool) -> None:
        self._project_available = bool(available)
        self._update_enabled_state()
        if not available:
            self.project_label.setText("Wybierz przygotowany projekt")
            self.feedback_label.setText("Historia pozostaje tylko do odczytu.")
            self._reset_rows()

    def set_project(self, alias: str, workspace_root: str) -> None:
        self.project_label.setText(f"{alias} · {workspace_root}")
        self.set_project_available(True)
        self.feedback_label.setText("Kliknij Odśwież historię, aby pobrać pierwszą ograniczoną stronę.")
        self._reset_rows()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = bool(busy)
        self._update_enabled_state()
        if message:
            self.feedback_label.setText(message)

    def apply_snapshot(self, snapshot: HistorySnapshot, *, append: bool) -> None:
        self._last_snapshot = snapshot
        self.set_busy(False)
        if not snapshot.ok:
            self.feedback_label.setText(
                f"{snapshot.error_code or 'unknown'} — {snapshot.error_message or 'brak szczegółów'}"
            )
            self._has_more = False
            self._update_enabled_state()
            return

        if append:
            known = {event.sequence for event in self._events}
            self._events.extend(event for event in snapshot.events if event.sequence not in known)
        else:
            self._events = list(snapshot.events)
        self._events.sort(key=lambda event: event.sequence)
        self._next_after_event_id = snapshot.cursor.next_after_event_id
        self._has_more = snapshot.cursor.has_more
        self._render_rows()
        self.feedback_label.setText(
            f"Zdarzenia: {len(self._events)} · następny kursor: {self._next_after_event_id} · "
            f"więcej: {'tak' if self._has_more else 'nie'}. Odczyt nie zmienił Journalu."
        )
        self._update_enabled_state()

    def smoke_report(self) -> dict[str, Any]:
        return {
            "history_view_present": True,
            "history_read_only": True,
            "history_pagination_present": self.load_more_button is not None,
            "history_filters_present": self.session_filter is not None and self.command_filter is not None,
            "history_loaded": self._last_snapshot is not None,
            "history_event_count": len(self._events),
        }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("HistoryHeroPanel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Historia Journalu")
        title.setObjectName("HeroTitle")
        self.project_label = QLabel("Wybierz przygotowany projekt")
        self.project_label.setObjectName("HeroText")
        self.project_label.setWordWrap(True)
        hero_layout.addWidget(title)
        hero_layout.addWidget(self.project_label)
        layout.addWidget(hero)

        filters = QFrame()
        filters.setObjectName("HistoryFiltersPanel")
        grid = QGridLayout(filters)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(QLabel("Session ID"), 0, 0)
        self.session_filter = QLineEdit()
        self.session_filter.setObjectName("HistorySessionFilter")
        self.session_filter.setPlaceholderText("opcjonalny exact session ID")
        grid.addWidget(self.session_filter, 1, 0)
        grid.addWidget(QLabel("Command ID"), 0, 1)
        self.command_filter = QLineEdit()
        self.command_filter.setObjectName("HistoryCommandFilter")
        self.command_filter.setPlaceholderText("opcjonalny exact command ID")
        grid.addWidget(self.command_filter, 1, 1)
        grid.addWidget(QLabel("Limit strony"), 0, 2)
        self.limit_spin = QSpinBox()
        self.limit_spin.setObjectName("HistoryLimitSpin")
        self.limit_spin.setRange(1, 500)
        self.limit_spin.setValue(100)
        grid.addWidget(self.limit_spin, 1, 2)
        self.refresh_button = QPushButton("Odśwież historię")
        self.refresh_button.setObjectName("RefreshHistoryButton")
        self.refresh_button.clicked.connect(self._request_first_page)
        grid.addWidget(self.refresh_button, 1, 3)
        self.load_more_button = QPushButton("Wczytaj więcej")
        self.load_more_button.setObjectName("LoadMoreHistoryButton")
        self.load_more_button.clicked.connect(self._request_next_page)
        grid.addWidget(self.load_more_button, 1, 4)
        layout.addWidget(filters)

        content = QHBoxLayout()
        content.setSpacing(12)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("HistoryTable")
        self.table.setHorizontalHeaderLabels(
            ["Seq", "Czas", "Severity", "Typ", "Session", "Command"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._render_selection)
        content.addWidget(self.table, 3)

        details_panel = QFrame()
        details_panel.setObjectName("HistoryDetailsPanel")
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(16, 14, 16, 14)
        details_title = QLabel("Szczegóły zdarzenia")
        details_title.setObjectName("HistorySectionTitle")
        self.details = QTextEdit()
        self.details.setObjectName("HistoryDetails")
        self.details.setReadOnly(True)
        self.details.setPlainText("Wybierz zdarzenie z tabeli.")
        details_layout.addWidget(details_title)
        details_layout.addWidget(self.details, 1)
        content.addWidget(details_panel, 2)
        layout.addLayout(content, 1)

        self.feedback_label = QLabel("Historia pozostaje tylko do odczytu.")
        self.feedback_label.setObjectName("HistoryFeedback")
        self.feedback_label.setWordWrap(True)
        layout.addWidget(self.feedback_label)

    def _request_first_page(self) -> None:
        self.query_requested.emit(self._query(after_event_id=0, append=False))

    def _request_next_page(self) -> None:
        if not self._has_more:
            return
        self.query_requested.emit(self._query(after_event_id=self._next_after_event_id, append=True))

    def _query(self, *, after_event_id: int, append: bool) -> dict[str, Any]:
        session_id = self.session_filter.text().strip() or None
        command_id = self.command_filter.text().strip() or None
        return {
            "after_event_id": after_event_id,
            "limit": self.limit_spin.value(),
            "session_id": session_id,
            "command_id": command_id,
            "append": append,
        }

    def _render_rows(self) -> None:
        self.table.setRowCount(len(self._events))
        for row, event in enumerate(self._events):
            values = (
                str(event.sequence),
                event.occurred_at,
                event.severity,
                event.event_type,
                event.session_id or "—",
                event.command_id or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, event.sequence)
                self.table.setItem(row, column, item)
        if self._events:
            self.table.selectRow(len(self._events) - 1)
        else:
            self.details.setPlainText("Brak zdarzeń dla wybranych filtrów.")

    def _render_selection(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if row < 0 or row >= len(self._events):
            return
        event = self._events[row]
        self.details.setPlainText(json.dumps(event.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))

    def _reset_rows(self) -> None:
        self._events = []
        self._next_after_event_id = 0
        self._has_more = False
        self._last_snapshot = None
        self.table.setRowCount(0)
        self.details.setPlainText("Wybierz zdarzenie z tabeli.")
        self._update_enabled_state()

    def _update_enabled_state(self) -> None:
        enabled = self._project_available and not self._busy
        self.refresh_button.setEnabled(enabled)
        self.session_filter.setEnabled(enabled)
        self.command_filter.setEnabled(enabled)
        self.limit_spin.setEnabled(enabled)
        self.load_more_button.setEnabled(enabled and self._has_more)
