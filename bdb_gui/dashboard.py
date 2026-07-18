from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .operations import ControlResult, ProjectStatusSnapshot


class RuntimeCard(QFrame):
    def __init__(self, title: str, value: str = "—", detail: str = "") -> None:
        super().__init__()
        self.setObjectName("RuntimeCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(5)

        title_label = QLabel(title)
        title_label.setObjectName("RuntimeCardTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("RuntimeCardValue")
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("RuntimeCardDetail")
        self.detail_label.setWordWrap(True)

        layout.addWidget(title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)

    def update_value(self, value: str, detail: str = "") -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)


class DashboardWidget(QWidget):
    refresh_status_requested = Signal()
    control_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DashboardPage")
        self._project_available = False
        self._busy = False
        self._build_ui()
        self.set_project_available(False)

    @property
    def arm_minutes(self) -> int:
        return int(self.arm_minutes_spin.value())

    def set_project_available(self, available: bool) -> None:
        self._project_available = bool(available)
        self._update_control_enabled_state()
        if not available:
            self.project_label.setText("Wybierz przygotowany projekt")
            self.overall_label.setText("BRAK PROJEKTU")
            self.bridge_card.update_value("—", "Brak wybranego workspace")
            self.native_card.update_value("—", "Brak wybranego workspace")
            self.promoter_card.update_value("—", "Brak wybranego workspace")
            self.source_card.update_value("—", "Brak wybranego workspace")

    def set_project(self, alias: str, workspace_root: str) -> None:
        self.project_label.setText(f"{alias} · {workspace_root}")
        self.set_project_available(True)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = bool(busy)
        self._update_control_enabled_state()
        if message:
            self.feedback_label.setText(message)
        self.feedback_label.setProperty("busy", busy)
        self.feedback_label.style().unpolish(self.feedback_label)
        self.feedback_label.style().polish(self.feedback_label)

    def apply_status(self, snapshot: ProjectStatusSnapshot) -> None:
        self.set_busy(False)
        if not snapshot.ok:
            self.overall_label.setText("STATUS NIEDOSTĘPNY")
            self.bridge_card.update_value("BŁĄD", snapshot.error_code or "unknown")
            self.native_card.update_value("—", "Nie odczytano")
            self.promoter_card.update_value("—", "Nie odczytano")
            self.source_card.update_value("—", "Nie odczytano")
            self.feedback_label.setText(snapshot.error_message or "Nie udało się odczytać statusu.")
            return

        self.overall_label.setText(snapshot.overall_status or "UNKNOWN")
        bridge_detail = _join_details(
            f"PID {snapshot.bridge_pid}" if snapshot.bridge_pid is not None else None,
            _bool_label("żywy", snapshot.bridge_pid_alive),
        )
        self.bridge_card.update_value(snapshot.bridge_status or "UNKNOWN", bridge_detail)

        native_detail = snapshot.native_armed_until or "Brak terminu uzbrojenia"
        self.native_card.update_value(_bool_value(snapshot.native_armed, "UZBROJONY", "ROZBROJONY"), native_detail)

        promoter_detail = f"PID {snapshot.promoter_pid}" if snapshot.promoter_pid is not None else "Brak PID"
        self.promoter_card.update_value(
            _bool_value(snapshot.promoter_running, "DZIAŁA", "ZATRZYMANY"),
            promoter_detail,
        )

        source_detail = snapshot.source_head or "Brak HEAD"
        self.source_card.update_value(
            _bool_value(snapshot.source_clean, "CZYSTE", "ZMIANY LOKALNE"),
            source_detail,
        )
        self.feedback_label.setText(
            "Status pobrany tylko do odczytu. Nie wykonano żadnej operacji sterującej."
        )

    def apply_control_result(self, result: ControlResult) -> None:
        if result.ok:
            action_label = {
                "start": "Start zakończony",
                "stop": "Stop zakończony",
                "rearm": "Native Host ponownie uzbrojony",
            }[result.action]
            self.feedback_label.setText(
                f"{action_label}. Trwa odświeżanie statusu potwierdzającego wynik."
            )
        else:
            self.feedback_label.setText(
                f"Operacja {result.action} nie powiodła się: "
                f"{result.error_code or 'unknown'} — {result.error_message or 'brak szczegółów'}"
            )

    def smoke_report(self) -> dict[str, Any]:
        return {
            "action_controls_present": all(
                button is not None
                for button in (self.start_button, self.stop_button, self.rearm_button)
            ),
            "confirmation_required": True,
            "arm_minutes_min": self.arm_minutes_spin.minimum(),
            "arm_minutes_max": self.arm_minutes_spin.maximum(),
            "project_available": self._project_available,
            "dashboard_busy": self._busy,
        }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        hero = QFrame()
        hero.setObjectName("HeroPanel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        hero_layout.setSpacing(7)
        heading = QLabel("Lokalny stan BDB i jawne sterowanie")
        heading.setObjectName("HeroTitle")
        self.project_label = QLabel("Wybierz przygotowany projekt")
        self.project_label.setObjectName("HeroText")
        self.project_label.setWordWrap(True)
        self.overall_label = QLabel("BRAK PROJEKTU")
        self.overall_label.setObjectName("OverallStatus")
        hero_layout.addWidget(heading)
        hero_layout.addWidget(self.project_label)
        hero_layout.addWidget(self.overall_label, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(hero)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.bridge_card = RuntimeCard("BRIDGE")
        self.native_card = RuntimeCard("NATIVE HOST")
        self.promoter_card = RuntimeCard("PROMOTER")
        self.source_card = RuntimeCard("SOURCE REPO")
        for card in (
            self.bridge_card,
            self.native_card,
            self.promoter_card,
            self.source_card,
        ):
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            cards.addWidget(card)
        layout.addLayout(cards)

        control_panel = QFrame()
        control_panel.setObjectName("ControlPanel")
        control_layout = QVBoxLayout(control_panel)
        control_layout.setContentsMargins(20, 18, 20, 18)
        control_layout.setSpacing(12)

        title = QLabel("STEROWANIE PROCESEM")
        title.setObjectName("ControlTitle")
        description = QLabel(
            "Każda mutacja wymaga osobnego kliknięcia i potwierdzenia. "
            "W trakcie jednej operacji pozostałe kontrolki są blokowane."
        )
        description.setObjectName("ControlDescription")
        description.setWordWrap(True)
        control_layout.addWidget(title)
        control_layout.addWidget(description)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.refresh_status_button = QPushButton("Odśwież status")
        self.refresh_status_button.setObjectName("RefreshStatusButton")
        self.refresh_status_button.clicked.connect(self.refresh_status_requested.emit)
        row.addWidget(self.refresh_status_button)

        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("StartButton")
        self.start_button.clicked.connect(lambda: self.control_requested.emit("start"))
        row.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("StopButton")
        self.stop_button.clicked.connect(lambda: self.control_requested.emit("stop"))
        row.addWidget(self.stop_button)

        self.rearm_button = QPushButton("Re-arm")
        self.rearm_button.setObjectName("RearmButton")
        self.rearm_button.clicked.connect(lambda: self.control_requested.emit("rearm"))
        row.addWidget(self.rearm_button)

        minutes_label = QLabel("Uzbrojenie:")
        minutes_label.setObjectName("ArmMinutesLabel")
        row.addWidget(minutes_label)
        self.arm_minutes_spin = QSpinBox()
        self.arm_minutes_spin.setObjectName("ArmMinutesSpin")
        self.arm_minutes_spin.setRange(1, 60)
        self.arm_minutes_spin.setValue(30)
        self.arm_minutes_spin.setSuffix(" min")
        row.addWidget(self.arm_minutes_spin)
        row.addStretch(1)
        control_layout.addLayout(row)

        self.feedback_label = QLabel("Wybierz projekt, aby odczytać status.")
        self.feedback_label.setObjectName("ControlFeedback")
        self.feedback_label.setWordWrap(True)
        control_layout.addWidget(self.feedback_label)
        layout.addWidget(control_panel)
        layout.addStretch(1)

    def _update_control_enabled_state(self) -> None:
        enabled = self._project_available and not self._busy
        self.refresh_status_button.setEnabled(enabled)
        self.start_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)
        self.rearm_button.setEnabled(enabled)
        self.arm_minutes_spin.setEnabled(enabled)


def _bool_value(value: bool | None, true_label: str, false_label: str) -> str:
    if value is None:
        return "UNKNOWN"
    return true_label if value else false_label


def _bool_label(label: str, value: bool | None) -> str | None:
    if value is None:
        return None
    return f"{label}: {'tak' if value else 'nie'}"


def _join_details(*values: str | None) -> str:
    return " · ".join(value for value in values if value)
