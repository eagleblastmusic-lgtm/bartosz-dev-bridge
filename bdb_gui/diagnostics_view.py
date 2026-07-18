from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .diagnostics import DiagnosticsExportResult, DiagnosticsSnapshot


class DiagnosticsWidget(QWidget):
    collect_requested = Signal()
    export_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DiagnosticsPage")
        self._project_available = False
        self._busy = False
        self._snapshot: DiagnosticsSnapshot | None = None
        self._last_export: DiagnosticsExportResult | None = None
        self._build_ui()
        self.set_project_available(False)

    @property
    def snapshot(self) -> DiagnosticsSnapshot | None:
        return self._snapshot

    @property
    def last_export(self) -> DiagnosticsExportResult | None:
        return self._last_export

    def set_project_available(self, available: bool) -> None:
        self._project_available = bool(available)
        self._update_enabled_state()
        if not available:
            self.project_label.setText("Wybierz przygotowany projekt")
            self.state_label.setText("BRAK SNAPSHOTU")
            self.feedback_label.setText("Zbieranie i eksport wymagają jawnego działania użytkownika.")
            self._clear()

    def set_project(self, alias: str, workspace_root: str) -> None:
        self.project_label.setText(f"{alias} · {workspace_root}")
        self.set_project_available(True)
        self.state_label.setText("NIEZEBRANE")
        self.feedback_label.setText("Kliknij Zbierz diagnostykę. Operacja pozostaje tylko do odczytu.")
        self._clear()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = bool(busy)
        self._update_enabled_state()
        if message:
            self.feedback_label.setText(message)

    def apply_snapshot(self, snapshot: DiagnosticsSnapshot) -> None:
        self._snapshot = snapshot
        self._last_export = None
        self.set_busy(False)
        self.state_label.setText("KOMPLETNY" if snapshot.complete else "CZĘŚCIOWY")
        self.table.setRowCount(len(snapshot.sections))
        for row, section in enumerate(snapshot.sections):
            error = "—" if section.ok else f"{section.error_code}: {section.error_message}"
            values = (section.name, "OK" if section.ok else "BŁĄD", section.operation_id, error)
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.details.setPlainText(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        self.feedback_label.setText(
            f"Snapshot {snapshot.generated_at}; sekcje {len(snapshot.sections)}; "
            f"redakcja {snapshot.redaction_version}. Można jawnie wyeksportować sanitizowany ZIP."
        )
        self._update_enabled_state()

    def apply_export_result(self, result: DiagnosticsExportResult) -> None:
        self._last_export = result
        self.set_busy(False)
        self.feedback_label.setText(
            f"Eksport zapisany: {result.output_path} · {result.size_bytes} B · {result.sha256}"
        )

    def apply_export_error(self, code: str, message: str) -> None:
        self.set_busy(False)
        self.feedback_label.setText(f"Eksport nieudany: {code} — {message}")

    def smoke_report(self) -> dict[str, Any]:
        return {
            "diagnostics_view_present": True,
            "diagnostics_collect_explicit": True,
            "diagnostics_export_explicit": True,
            "diagnostics_snapshot_loaded": self._snapshot is not None,
            "diagnostics_export_completed": self._last_export is not None,
        }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("DiagnosticsHeroPanel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Diagnostyka i eksport")
        title.setObjectName("HeroTitle")
        self.project_label = QLabel("Wybierz przygotowany projekt")
        self.project_label.setObjectName("HeroText")
        self.project_label.setWordWrap(True)
        self.state_label = QLabel("BRAK SNAPSHOTU")
        self.state_label.setObjectName("DiagnosticsState")
        hero_layout.addWidget(title)
        hero_layout.addWidget(self.project_label)
        hero_layout.addWidget(self.state_label)
        layout.addWidget(hero)

        toolbar = QFrame()
        toolbar.setObjectName("DiagnosticsToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(16, 12, 16, 12)
        toolbar_text = QLabel(
            "Bounded tails, status, capabilities i current operation. Bez Journal DB i plików repo."
        )
        toolbar_text.setObjectName("DiagnosticsHint")
        toolbar_text.setWordWrap(True)
        toolbar_layout.addWidget(toolbar_text, 1)
        self.collect_button = QPushButton("Zbierz diagnostykę")
        self.collect_button.setObjectName("CollectDiagnosticsButton")
        self.collect_button.clicked.connect(self.collect_requested.emit)
        toolbar_layout.addWidget(self.collect_button)
        self.export_button = QPushButton("Eksportuj ZIP")
        self.export_button.setObjectName("ExportDiagnosticsButton")
        self.export_button.clicked.connect(self.export_requested.emit)
        toolbar_layout.addWidget(self.export_button)
        layout.addWidget(toolbar)

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("DiagnosticsTable")
        self.table.setHorizontalHeaderLabels(["Sekcja", "Stan", "Operation ID", "Błąd"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        self.details = QTextEdit()
        self.details.setObjectName("DiagnosticsDetails")
        self.details.setReadOnly(True)
        self.details.setPlainText("Snapshot diagnostyczny nie został jeszcze zebrany.")
        layout.addWidget(self.details, 2)

        self.feedback_label = QLabel("Zbieranie i eksport wymagają jawnego działania użytkownika.")
        self.feedback_label.setObjectName("DiagnosticsFeedback")
        self.feedback_label.setWordWrap(True)
        layout.addWidget(self.feedback_label)

    def _clear(self) -> None:
        self._snapshot = None
        self._last_export = None
        self.table.setRowCount(0)
        self.details.setPlainText("Snapshot diagnostyczny nie został jeszcze zebrany.")
        self._update_enabled_state()

    def _update_enabled_state(self) -> None:
        self.collect_button.setEnabled(self._project_available and not self._busy)
        self.export_button.setEnabled(
            self._project_available and not self._busy and self._snapshot is not None
        )
