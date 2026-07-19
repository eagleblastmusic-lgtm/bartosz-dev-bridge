from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .session_history import SessionAttempt, SessionHistorySnapshot, SessionSummary


PathOpener = Callable[[str], bool]


class SessionHistoryWidget(QWidget):
    refresh_requested = Signal(int)

    def __init__(self, *, path_opener: PathOpener | None = None) -> None:
        super().__init__()
        self.setObjectName("SessionHistoryPage")
        self._path_opener = path_opener or _open_local_path
        self._project_available = False
        self._busy = False
        self._sessions: list[SessionSummary] = []
        self._last_snapshot: SessionHistorySnapshot | None = None
        self._selected_session: SessionSummary | None = None
        self._selected_attempt: SessionAttempt | None = None
        self._build_ui()
        self.set_project_available(False)

    @property
    def last_snapshot(self) -> SessionHistorySnapshot | None:
        return self._last_snapshot

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def set_project_available(self, available: bool) -> None:
        self._project_available = bool(available)
        if not available:
            self.project_label.setText("Wybierz przygotowany projekt")
            self.feedback_label.setText("Historia sesji pozostaje tylko do odczytu.")
            self._reset()
        self._update_enabled_state()

    def set_project(self, alias: str, workspace_root: str) -> None:
        self.project_label.setText(f"{alias} · {workspace_root}")
        self._project_available = True
        self.feedback_label.setText("Kliknij Odśwież sesje, aby pobrać bounded podsumowania.")
        self._reset()
        self._update_enabled_state()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = bool(busy)
        self._update_enabled_state()
        if message:
            self.feedback_label.setText(message)

    def apply_snapshot(self, snapshot: SessionHistorySnapshot) -> None:
        self._last_snapshot = snapshot
        self.set_busy(False)
        if not snapshot.ok:
            self._sessions = []
            self._render_rows()
            self.feedback_label.setText(
                f"{snapshot.error_code or 'unknown'} — {snapshot.error_message or 'brak szczegółów'}"
            )
            return
        self._sessions = list(snapshot.sessions)
        self._render_rows()
        self.feedback_label.setText(
            f"Sesje: {len(self._sessions)} · limit: {snapshot.limit}. "
            "Relacje naprawcze między różnymi sesjami nie są zgadywane."
        )

    def smoke_report(self) -> dict[str, Any]:
        return {
            "session_history_view_present": True,
            "session_history_read_only": True,
            "session_history_loaded": self._last_snapshot is not None,
            "session_history_count": len(self._sessions),
            "session_result_open_explicit": True,
            "session_receipt_open_explicit": True,
            "session_folder_open_explicit": True,
            "session_repair_relationships_inferred": False,
        }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        hero = QFrame()
        hero.setObjectName("SessionHistoryHeroPanel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Zakończone i historyczne sesje")
        title.setObjectName("HeroTitle")
        self.project_label = QLabel("Wybierz przygotowany projekt")
        self.project_label.setObjectName("HeroText")
        self.project_label.setWordWrap(True)
        notice = QLabel(
            "Każda sesja jest pokazywana osobno. Control Center nie łączy automatycznie "
            "nieudanej sesji z późniejszą sesją naprawczą."
        )
        notice.setObjectName("SessionHistoryNotice")
        notice.setWordWrap(True)
        hero_layout.addWidget(title)
        hero_layout.addWidget(self.project_label)
        hero_layout.addWidget(notice)
        layout.addWidget(hero)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Limit sesji"))
        self.limit_spin = QSpinBox()
        self.limit_spin.setObjectName("SessionHistoryLimitSpin")
        self.limit_spin.setRange(1, 100)
        self.limit_spin.setValue(20)
        toolbar.addWidget(self.limit_spin)
        self.refresh_button = QPushButton("Odśwież sesje")
        self.refresh_button.setObjectName("RefreshSessionHistoryButton")
        self.refresh_button.clicked.connect(lambda: self.refresh_requested.emit(self.limit_spin.value()))
        toolbar.addWidget(self.refresh_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        content = QHBoxLayout()
        content.setSpacing(12)
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("SessionHistoryTable")
        self.table.setHorizontalHeaderLabels(
            ["Zaktualizowano", "Session", "Stan", "Próby", "Wynik", "Checkpoint", "Promocja"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._render_selection)
        content.addWidget(self.table, 3)

        details_panel = QFrame()
        details_panel.setObjectName("SessionHistoryDetailsPanel")
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(14, 12, 14, 12)
        details_title = QLabel("Podsumowanie wybranej sesji")
        details_title.setObjectName("HistorySectionTitle")
        self.details = QTextEdit()
        self.details.setObjectName("SessionHistoryDetails")
        self.details.setReadOnly(True)
        self.details.setPlainText("Wybierz sesję z tabeli.")
        details_layout.addWidget(details_title)
        details_layout.addWidget(self.details, 1)

        buttons = QHBoxLayout()
        self.open_result_button = QPushButton("Otwórz wynik")
        self.open_result_button.setObjectName("OpenSessionResultButton")
        self.open_result_button.clicked.connect(self._open_result)
        self.open_receipt_button = QPushButton("Otwórz receipt")
        self.open_receipt_button.setObjectName("OpenSessionReceiptButton")
        self.open_receipt_button.clicked.connect(self._open_receipt)
        self.open_folder_button = QPushButton("Otwórz katalog")
        self.open_folder_button.setObjectName("OpenSessionFolderButton")
        self.open_folder_button.clicked.connect(self._open_folder)
        for button in (self.open_result_button, self.open_receipt_button, self.open_folder_button):
            buttons.addWidget(button)
        details_layout.addLayout(buttons)
        content.addWidget(details_panel, 2)
        layout.addLayout(content, 1)

        self.feedback_label = QLabel("Historia sesji pozostaje tylko do odczytu.")
        self.feedback_label.setObjectName("SessionHistoryFeedback")
        self.feedback_label.setWordWrap(True)
        layout.addWidget(self.feedback_label)
        self._update_open_buttons()

    def _render_rows(self) -> None:
        self.table.setRowCount(len(self._sessions))
        for row, session in enumerate(self._sessions):
            latest = session.latest_attempt
            values = (
                session.updated_at,
                session.session_id,
                session.state,
                str(len(session.attempts)),
                latest.result_status if latest and latest.result_status else "—",
                latest.checkpoint_state if latest and latest.checkpoint_state else "—",
                latest.promotion_status if latest else "—",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        if self._sessions:
            self.table.selectRow(0)
        else:
            self._selected_session = None
            self._selected_attempt = None
            self.details.setPlainText("Brak sesji dla wybranego projektu.")
            self._update_open_buttons()

    def _render_selection(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._selected_session = None
            self._selected_attempt = None
            self._update_open_buttons()
            return
        row = rows[0].row()
        if row < 0 or row >= len(self._sessions):
            return
        self._selected_session = self._sessions[row]
        self._selected_attempt = self._selected_session.latest_attempt
        self.details.setPlainText(
            json.dumps(self._selected_session.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        self._update_open_buttons()

    def _open_result(self) -> None:
        attempt = self._selected_attempt
        if attempt is not None and attempt.result_file.valid and attempt.result_file.path:
            self._open(attempt.result_file.path, "wynik")

    def _open_receipt(self) -> None:
        attempt = self._selected_attempt
        if attempt is not None and attempt.receipt_file.valid and attempt.receipt_file.path:
            self._open(attempt.receipt_file.path, "receipt")

    def _open_folder(self) -> None:
        attempt = self._selected_attempt
        if attempt is None:
            return
        source = None
        if attempt.result_file.valid and attempt.result_file.path:
            source = attempt.result_file.path
        elif attempt.receipt_file.valid and attempt.receipt_file.path:
            source = attempt.receipt_file.path
        if source:
            self._open(str(Path(source).parent), "katalog")

    def _open(self, path: str, label: str) -> None:
        opened = bool(self._path_opener(path))
        self.feedback_label.setText(
            f"Otwarto {label}: {path}" if opened else f"Nie udało się otworzyć {label}: {path}"
        )

    def _reset(self) -> None:
        self._sessions = []
        self._last_snapshot = None
        self._selected_session = None
        self._selected_attempt = None
        self.table.setRowCount(0)
        self.details.setPlainText("Wybierz sesję z tabeli.")
        self._update_open_buttons()

    def _update_enabled_state(self) -> None:
        enabled = self._project_available and not self._busy
        self.refresh_button.setEnabled(enabled)
        self.limit_spin.setEnabled(enabled)
        self._update_open_buttons()

    def _update_open_buttons(self) -> None:
        attempt = self._selected_attempt
        enabled = self._project_available and not self._busy and attempt is not None
        result_ok = bool(enabled and attempt and attempt.result_file.valid and attempt.result_file.path)
        receipt_ok = bool(enabled and attempt and attempt.receipt_file.valid and attempt.receipt_file.path)
        folder_ok = result_ok or receipt_ok
        self.open_result_button.setEnabled(result_ok)
        self.open_receipt_button.setEnabled(receipt_ok)
        self.open_folder_button.setEnabled(folder_ok)


def _open_local_path(path: str) -> bool:
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))
