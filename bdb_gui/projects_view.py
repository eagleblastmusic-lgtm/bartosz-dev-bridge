from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .project_creator import ProjectCreatorResult
from .projects import PreparePlan, PrepareResult
from .runtime_paths import default_python_executable


class ProjectsWidget(QWidget):
    plan_requested = Signal(object)
    prepare_requested = Signal(object)
    creator_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("ProjectsPage")
        self._busy = False
        self._plan: PreparePlan | None = None
        self._build_ui()
        self._connect_invalidation()
        self._update_enabled_state()

    @property
    def plan(self) -> PreparePlan | None:
        return self._plan

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = bool(busy)
        self._update_enabled_state()
        if message:
            self.feedback_label.setText(message)

    def apply_plan(self, plan: PreparePlan) -> None:
        self.ack_checkbox.setChecked(False)
        self._plan = plan
        self.set_busy(False)
        self.plan_preview.setPlainText(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        self.plan_state.setText("PLAN GOTOWY — WYMAGA POTWIERDZENIA")
        self.feedback_label.setText(
            "Plan jest tylko do odczytu. Git preflight i zapis wykona istniejący preparer dopiero po potwierdzeniu."
        )
        self._update_enabled_state()

    def apply_plan_error(self, code: str, message: str) -> None:
        self.ack_checkbox.setChecked(False)
        self._plan = None
        self.set_busy(False)
        self.plan_state.setText("PLAN NIEPRAWIDŁOWY")
        self.plan_preview.setPlainText(f"{code}: {message}")
        self.feedback_label.setText("Nie wykonano żadnej mutacji.")
        self._update_enabled_state()

    def apply_prepare_result(self, result: PrepareResult) -> None:
        self.set_busy(False)
        self.plan_preview.setPlainText(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        self.ack_checkbox.setChecked(False)
        if result.ok:
            self.plan_state.setText("PROJEKT PRZYGOTOWANY")
            self.feedback_label.setText(
                f"Operator API potwierdził przygotowanie aliasu {result.project_alias or result.plan.alias}."
            )
            self._plan = None
        else:
            self.plan_state.setText("PREPARE NIEUDANY")
            self.feedback_label.setText(
                f"{result.error_code or 'prepare_failed'} — {result.error_message or 'brak szczegółów'}"
            )
        self._update_enabled_state()

    def apply_creator_result(self, result: ProjectCreatorResult) -> None:
        self.set_busy(False)
        self.plan_preview.setPlainText(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        if result.ok:
            self.plan_state.setText("PROJEKT UTWORZONY I URUCHOMIONY")
            self.feedback_label.setText(
                "Repozytorium i workspace są gotowe. Bridge działa, Native Host jest uzbrojony, a prompt przekazano do ChatGPT."
            )
        else:
            self.plan_state.setText("KREATOR NIE ZAKOŃCZYŁ PRACY")
            self.feedback_label.setText(
                f"{result.error_code or 'project_creator_failed'} — {result.error_message or 'brak szczegółów'}"
            )
        self._update_enabled_state()

    def smoke_report(self) -> dict[str, Any]:
        return {
            "projects_wizard_present": True,
            "project_creator_button_present": hasattr(self, "creator_button"),
            "prepare_plan_required": True,
            "prepare_confirmation_required": True,
            "prepare_plan_loaded": self._plan is not None,
        }

    def request_payload(self) -> dict[str, Any]:
        return {
            "alias": self.alias_edit.text(),
            "source_repo": self.source_edit.text(),
            "allowed_paths": self.allowed_paths_edit.toPlainText().splitlines(),
            "python_executable": self.python_edit.text(),
            "test_timeout_seconds": self.test_timeout_spin.value(),
        }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("ProjectsHeroPanel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        hero_header = QHBoxLayout()
        hero_text = QVBoxLayout()
        title = QLabel("Projekty BDB")
        title.setObjectName("HeroTitle")
        description = QLabel(
            "Kreator projektu wykonuje cały przebieg: nowe lub istniejące repo → Prepare → Start → Re-arm → prompt w ChatGPT. "
            "Niżej pozostaje ręczny tryb Prepare dla zaawansowanej konfiguracji."
        )
        description.setObjectName("HeroText")
        description.setWordWrap(True)
        hero_text.addWidget(title)
        hero_text.addWidget(description)
        hero_header.addLayout(hero_text, 1)
        self.creator_button = QPushButton("Kreator projektu")
        self.creator_button.setObjectName("OpenProjectCreatorButton")
        self.creator_button.clicked.connect(lambda _checked=False: self.creator_requested.emit())
        hero_header.addWidget(self.creator_button)
        self.plan_state = QLabel("BRAK PLANU")
        self.plan_state.setObjectName("ProjectsPlanState")
        hero_layout.addLayout(hero_header)
        hero_layout.addWidget(self.plan_state)
        layout.addWidget(hero)

        body = QHBoxLayout()
        body.setSpacing(14)

        form_panel = QFrame()
        form_panel.setObjectName("ProjectsFormPanel")
        form_layout = QFormLayout(form_panel)
        form_layout.setContentsMargins(20, 18, 20, 18)
        form_layout.setSpacing(10)
        self.alias_edit = QLineEdit()
        self.alias_edit.setObjectName("ProjectAliasEdit")
        self.alias_edit.setPlaceholderText("np. gicleeart")
        form_layout.addRow("Alias", self.alias_edit)
        self.source_edit = QLineEdit()
        self.source_edit.setObjectName("ProjectSourceEdit")
        self.source_edit.setPlaceholderText("C:\\Strona\\projekt")
        form_layout.addRow("Source repo", self.source_edit)
        self.allowed_paths_edit = QTextEdit()
        self.allowed_paths_edit.setObjectName("ProjectAllowedPathsEdit")
        self.allowed_paths_edit.setPlaceholderText("README.md\ntests/*.py\nsrc/**")
        self.allowed_paths_edit.setMaximumHeight(115)
        form_layout.addRow("Allowed paths", self.allowed_paths_edit)
        self.python_edit = QLineEdit(default_python_executable())
        self.python_edit.setPlaceholderText("Wskaż python.exe środowiska BDB")
        self.python_edit.setObjectName("ProjectPythonEdit")
        form_layout.addRow("Python", self.python_edit)
        self.test_timeout_spin = QSpinBox()
        self.test_timeout_spin.setRange(1, 3600)
        self.test_timeout_spin.setValue(120)
        self.test_timeout_spin.setGroupSeparatorShown(True)
        form_layout.addRow("Test timeout (s)", self.test_timeout_spin)
        body.addWidget(form_panel, 2)

        preview_panel = QFrame()
        preview_panel.setObjectName("ProjectsPreviewPanel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(18, 16, 18, 16)
        preview_title = QLabel("Plan / wynik")
        preview_title.setObjectName("ProjectsSectionTitle")
        self.plan_preview = QTextEdit()
        self.plan_preview.setObjectName("ProjectsPlanPreview")
        self.plan_preview.setReadOnly(True)
        self.plan_preview.setPlainText("Uruchom Kreator projektu albo uzupełnij ręczny formularz Prepare.")
        preview_layout.addWidget(preview_title)
        preview_layout.addWidget(self.plan_preview, 1)
        body.addWidget(preview_panel, 3)
        layout.addLayout(body, 1)

        actions = QHBoxLayout()
        self.ack_checkbox = QCheckBox(
            "Rozumiem, że Prepare utworzy lokalny control repo/workspace i zaktualizuje konfigurację Native Host."
        )
        self.ack_checkbox.setObjectName("PrepareAckCheckbox")
        actions.addWidget(self.ack_checkbox, 1)
        self.plan_button = QPushButton("Zbuduj plan")
        self.plan_button.setObjectName("BuildPreparePlanButton")
        self.plan_button.clicked.connect(
            lambda _checked=False: self.plan_requested.emit(self.request_payload())
        )
        actions.addWidget(self.plan_button)
        self.prepare_button = QPushButton("Przygotuj projekt")
        self.prepare_button.setObjectName("ExecutePrepareButton")
        self.prepare_button.clicked.connect(self._emit_prepare)
        actions.addWidget(self.prepare_button)
        layout.addLayout(actions)

        self.feedback_label = QLabel("Nie wykonano żadnej operacji.")
        self.feedback_label.setObjectName("ProjectsFeedback")
        self.feedback_label.setWordWrap(True)
        layout.addWidget(self.feedback_label)

    def _connect_invalidation(self) -> None:
        for edit in (self.alias_edit, self.source_edit, self.python_edit):
            edit.textChanged.connect(self._invalidate_plan)
        self.allowed_paths_edit.textChanged.connect(self._invalidate_plan)
        self.test_timeout_spin.valueChanged.connect(self._invalidate_plan)
        self.ack_checkbox.toggled.connect(self._update_enabled_state)

    def _invalidate_plan(self, *_args: object) -> None:
        if self._plan is None:
            return
        self._plan = None
        self.ack_checkbox.setChecked(False)
        self.plan_state.setText("PLAN WYMAGA PONOWNEJ WALIDACJI")
        self.feedback_label.setText("Zmiana formularza unieważniła poprzedni plan.")
        self._update_enabled_state()

    def _emit_prepare(self, *_args: object) -> None:
        if self._plan is not None and self.ack_checkbox.isChecked():
            self.prepare_requested.emit(self._plan)

    def _update_enabled_state(self, *_args: object) -> None:
        self.creator_button.setEnabled(not self._busy)
        self.plan_button.setEnabled(not self._busy)
        self.prepare_button.setEnabled(
            not self._busy and self._plan is not None and self.ack_checkbox.isChecked()
        )
        self.ack_checkbox.setEnabled(not self._busy and self._plan is not None)
