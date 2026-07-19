from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .current_operation import CurrentOperationSnapshot
from .operation_flow import OperationFlow, OperationFlowStep, build_operation_flow, empty_operation_flow


class FlowStepRow(QFrame):
    def __init__(self, step: OperationFlowStep) -> None:
        super().__init__()
        self.setObjectName("OperationFlowStep")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(12)

        self.status_label = QLabel()
        self.status_label.setObjectName("OperationFlowStepStatus")
        self.status_label.setFixedWidth(86)
        self.title_label = QLabel(step.label)
        self.title_label.setObjectName("OperationFlowStepTitle")
        self.detail_label = QLabel()
        self.detail_label.setObjectName("OperationFlowStepDetail")
        self.detail_label.setWordWrap(True)

        layout.addWidget(self.status_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.detail_label, 1)
        self.apply_step(step)

    def apply_step(self, step: OperationFlowStep) -> None:
        self.setProperty("flowStatus", step.status)
        self.status_label.setText(_flow_status_label(step.status))
        self.title_label.setText(step.label)
        self.detail_label.setText(step.detail)
        self.style().unpolish(self)
        self.style().polish(self)


class CurrentOperationWidget(QWidget):
    refresh_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("CurrentOperationPage")
        self._project_available = False
        self._busy = False
        self._last_snapshot: CurrentOperationSnapshot | None = None
        self._flow = empty_operation_flow()
        self._build_ui()
        self.set_project_available(False)

    @property
    def last_snapshot(self) -> CurrentOperationSnapshot | None:
        return self._last_snapshot

    @property
    def flow(self) -> OperationFlow:
        return self._flow

    def set_project_available(self, available: bool) -> None:
        self._project_available = bool(available)
        self._update_enabled_state()
        if not available:
            self.project_label.setText("Wybierz przygotowany projekt")
            self.state_label.setText("BRAK PROJEKTU")
            self.feedback_label.setText("Widok nie wykonuje żadnej mutacji.")
            self._clear_details()
            self._apply_flow(empty_operation_flow())

    def set_project(self, alias: str, workspace_root: str) -> None:
        self.project_label.setText(f"{alias} · {workspace_root}")
        self.set_project_available(True)
        self.state_label.setText("NIEODCZYTANE")
        self.feedback_label.setText("Kliknij Odśwież lub poczekaj na pierwszy jawny odczyt.")
        self._clear_details()
        self._apply_flow(empty_operation_flow())

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = bool(busy)
        self._update_enabled_state()
        if message:
            self.feedback_label.setText(message)

    def apply_snapshot(self, snapshot: CurrentOperationSnapshot) -> None:
        self._last_snapshot = snapshot
        self.set_busy(False)
        if not snapshot.ok:
            self.state_label.setText("ODCZYT NIEDOSTĘPNY")
            self.feedback_label.setText(
                f"{snapshot.error_code or 'unknown'} — {snapshot.error_message or 'brak szczegółów'}"
            )
            self._clear_details()
            self._apply_flow(empty_operation_flow())
            return

        if not snapshot.active or snapshot.operation is None:
            self.state_label.setText("BRAK AKTYWNEJ OPERACJI")
            self.feedback_label.setText(
                "Journal nie zawiera aktywnej komendy. Odczyt był tylko do odczytu."
            )
            self._clear_details()
            self.generated_value.setText(snapshot.generated_at or "—")
            self._apply_flow(empty_operation_flow())
            return

        operation = snapshot.operation
        self.state_label.setText(operation.state.upper())
        values = {
            "command": operation.command_id,
            "session": operation.session_id,
            "sequence": str(operation.sequence),
            "operation": operation.operation or "—",
            "target": operation.target_path or "—",
            "profile": operation.profile_id or "—",
            "repository": operation.repository_id or "—",
            "session_state": operation.session_state or "—",
            "revision": str(operation.workspace_revision)
            if operation.workspace_revision is not None
            else "—",
            "state_hash": operation.workspace_state_hash or "—",
            "result": operation.result_status or "—",
            "error": operation.error_code or "—",
            "created": operation.created_at or "—",
            "updated": operation.updated_at or "—",
            "generated": snapshot.generated_at or "—",
        }
        for key, value in values.items():
            self._values[key].setText(value)
        self._apply_flow(build_operation_flow(operation))
        self.feedback_label.setText(
            "Aktywna operacja została odczytana z read-only projekcji Journalu."
        )

    def smoke_report(self) -> dict[str, Any]:
        snapshot = self._last_snapshot
        return {
            "current_operation_view_present": True,
            "current_operation_read_only": True,
            "current_operation_refresh_present": self.refresh_button is not None,
            "current_operation_loaded": snapshot is not None,
            "current_operation_active": snapshot.active if snapshot is not None and snapshot.ok else None,
            "operation_flow_present": len(self._flow_rows) == 6,
            "operation_flow_status": self._flow.overall_status,
        }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        header = QFrame()
        header.setObjectName("OperationHeroPanel")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 20, 24, 20)
        header_layout.setSpacing(7)
        title = QLabel("Bieżąca operacja BDB")
        title.setObjectName("HeroTitle")
        self.project_label = QLabel("Wybierz przygotowany projekt")
        self.project_label.setObjectName("HeroText")
        self.project_label.setWordWrap(True)
        self.state_label = QLabel("BRAK PROJEKTU")
        self.state_label.setObjectName("OperationState")
        header_layout.addWidget(title)
        header_layout.addWidget(self.project_label)
        header_layout.addWidget(self.state_label)
        layout.addWidget(header)

        flow_panel = QFrame()
        flow_panel.setObjectName("OperationFlowPanel")
        flow_layout = QVBoxLayout(flow_panel)
        flow_layout.setContentsMargins(22, 18, 22, 18)
        flow_layout.setSpacing(9)
        flow_title = QLabel("PRZEBIEG OPERACJI")
        flow_title.setObjectName("OperationSectionTitle")
        self.flow_summary_label = QLabel(self._flow.summary)
        self.flow_summary_label.setObjectName("OperationFlowSummary")
        self.flow_summary_label.setWordWrap(True)
        flow_layout.addWidget(flow_title)
        flow_layout.addWidget(self.flow_summary_label)
        self._flow_rows: dict[str, FlowStepRow] = {}
        for step in self._flow.steps:
            row = FlowStepRow(step)
            self._flow_rows[step.key] = row
            flow_layout.addWidget(row)
        layout.addWidget(flow_panel)

        panel = QFrame()
        panel.setObjectName("OperationDetailsPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 20, 22, 20)
        panel_layout.setSpacing(14)

        toolbar = QHBoxLayout()
        toolbar_title = QLabel("READ-ONLY JOURNAL PROJECTION")
        toolbar_title.setObjectName("OperationSectionTitle")
        toolbar.addWidget(toolbar_title)
        toolbar.addStretch(1)
        self.refresh_button = QPushButton("Odśwież operację")
        self.refresh_button.setObjectName("RefreshOperationButton")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(self.refresh_button)
        panel_layout.addLayout(toolbar)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        fields = (
            ("command", "Command ID"),
            ("session", "Session ID"),
            ("sequence", "Sekwencja"),
            ("operation", "Operacja"),
            ("target", "Ścieżka docelowa"),
            ("profile", "Profil testowy"),
            ("repository", "Repository ID"),
            ("session_state", "Stan sesji"),
            ("revision", "Rewizja workspace"),
            ("state_hash", "State hash"),
            ("result", "Status wyniku"),
            ("error", "Kod błędu"),
            ("created", "Utworzono"),
            ("updated", "Zaktualizowano"),
            ("generated", "Odczyt wygenerowano"),
        )
        self._values: dict[str, QLabel] = {}
        for row, (key, label_text) in enumerate(fields):
            label = QLabel(label_text)
            label.setObjectName("OperationFieldLabel")
            value = QLabel("—")
            value.setObjectName("OperationFieldValue")
            value.setTextInteractionFlags(value.textInteractionFlags())
            value.setWordWrap(True)
            grid.addWidget(label, row, 0)
            grid.addWidget(value, row, 1)
            self._values[key] = value
        self.generated_value = self._values["generated"]
        panel_layout.addLayout(grid)

        self.feedback_label = QLabel("Widok nie wykonuje żadnej mutacji.")
        self.feedback_label.setObjectName("OperationFeedback")
        self.feedback_label.setWordWrap(True)
        panel_layout.addWidget(self.feedback_label)
        layout.addWidget(panel)
        layout.addStretch(1)

    def _apply_flow(self, flow: OperationFlow) -> None:
        self._flow = flow
        self.flow_summary_label.setText(flow.summary)
        for step in flow.steps:
            self._flow_rows[step.key].apply_step(step)

    def _clear_details(self) -> None:
        for value in self._values.values():
            value.setText("—")

    def _update_enabled_state(self) -> None:
        self.refresh_button.setEnabled(self._project_available and not self._busy)


def _flow_status_label(status: str) -> str:
    return {
        "pending": "OCZEKUJE",
        "active": "TRWA",
        "success": "GOTOWE",
        "failed": "BŁĄD",
    }.get(status, status.upper())
