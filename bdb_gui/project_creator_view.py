from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from .project_creator import DEFAULT_ALLOWED_PATHS
from .runtime_paths import default_python_executable


class ProjectCreatorDialog(QDialog):
    submitted = Signal(object)

    def __init__(self, *, parent=None, default_projects_root: str | Path | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ProjectCreatorDialog")
        self.setWindowTitle("Kreator projektu BDB")
        self.resize(760, 760)
        self.setMinimumSize(660, 650)
        self._default_projects_root = Path(default_projects_root or (Path.home() / "BDB Projects"))
        self._build_ui()
        self._mode_changed()
        self._update_submit_state()

    def payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode_combo.currentData(),
            "alias": self.alias_edit.text(),
            "project_name": self.name_edit.text(),
            "projects_root": self.projects_root_edit.text(),
            "source_input": self.source_edit.text(),
            "github_visibility": self.visibility_combo.currentData(),
            "prompt": self.prompt_edit.toPlainText(),
            "auto_send": self.auto_send_checkbox.isChecked(),
            "allowed_paths": [
                line.strip()
                for line in self.allowed_paths_edit.toPlainText().splitlines()
                if line.strip()
            ],
            "python_executable": self.python_edit.text(),
            "test_timeout_seconds": self.test_timeout_spin.value(),
            "arm_minutes": self.arm_minutes_spin.value(),
        }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel("Kreator projektu")
        title.setObjectName("ProjectCreatorTitle")
        description = QLabel(
            "Jedno zatwierdzenie tworzy lub podłącza repozytorium, przygotowuje BDB, uruchamia Bridge, "
            "uzbraja Native Host i przekazuje zadanie do bieżącej rozmowy ChatGPT."
        )
        description.setWordWrap(True)
        description.setObjectName("ProjectCreatorDescription")
        layout.addWidget(title)
        layout.addWidget(description)

        form = QFormLayout()
        form.setSpacing(10)
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("ProjectCreatorMode")
        self.mode_combo.addItem("Nowy projekt + nowe repo GitHub", "new")
        self.mode_combo.addItem("Istniejący projekt", "existing")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Tryb", self.mode_combo)

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("ProjectCreatorName")
        self.name_edit.setPlaceholderText("np. kalkulator")
        form.addRow("Nazwa projektu / repo", self.name_edit)

        self.alias_edit = QLineEdit()
        self.alias_edit.setObjectName("ProjectCreatorAlias")
        self.alias_edit.setPlaceholderText("np. kalkulator")
        form.addRow("Alias BDB", self.alias_edit)

        projects_row = QHBoxLayout()
        self.projects_root_edit = QLineEdit(str(self._default_projects_root))
        self.projects_root_edit.setObjectName("ProjectCreatorProjectsRoot")
        browse = QPushButton("Wybierz…")
        browse.setObjectName("ProjectCreatorBrowseRoot")
        browse.clicked.connect(self._browse_projects_root)
        projects_row.addWidget(self.projects_root_edit, 1)
        projects_row.addWidget(browse)
        form.addRow("Katalog projektów", projects_row)

        self.source_edit = QLineEdit()
        self.source_edit.setObjectName("ProjectCreatorSource")
        self.source_edit.setPlaceholderText("C:\\Projekty\\aplikacja lub URL GitHub")
        self.source_label = QLabel("Źródło istniejącego projektu")
        form.addRow(self.source_label, self.source_edit)

        self.visibility_combo = QComboBox()
        self.visibility_combo.setObjectName("ProjectCreatorVisibility")
        self.visibility_combo.addItem("Prywatne", "private")
        self.visibility_combo.addItem("Publiczne", "public")
        self.visibility_label = QLabel("Widoczność GitHub")
        form.addRow(self.visibility_label, self.visibility_combo)

        self.python_edit = QLineEdit(default_python_executable())
        self.python_edit.setObjectName("ProjectCreatorPython")
        form.addRow("Python BDB", self.python_edit)

        self.test_timeout_spin = QSpinBox()
        self.test_timeout_spin.setRange(1, 3600)
        self.test_timeout_spin.setValue(180)
        form.addRow("Limit testów (s)", self.test_timeout_spin)

        self.arm_minutes_spin = QSpinBox()
        self.arm_minutes_spin.setRange(1, 60)
        self.arm_minutes_spin.setValue(30)
        form.addRow("Re-arm (min)", self.arm_minutes_spin)
        layout.addLayout(form)

        prompt_label = QLabel("Prompt startowy")
        prompt_label.setObjectName("ProjectCreatorPromptLabel")
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setObjectName("ProjectCreatorPrompt")
        self.prompt_edit.setPlaceholderText("np. stwórz kalkulator z testami i prostym interfejsem")
        self.prompt_edit.setMinimumHeight(120)
        layout.addWidget(prompt_label)
        layout.addWidget(self.prompt_edit)

        allowed_label = QLabel("Dozwolone ścieżki")
        allowed_label.setObjectName("ProjectCreatorAllowedLabel")
        self.allowed_paths_edit = QTextEdit()
        self.allowed_paths_edit.setPlainText("\n".join(DEFAULT_ALLOWED_PATHS))
        self.allowed_paths_edit.setObjectName("ProjectCreatorAllowedPaths")
        self.allowed_paths_edit.setMaximumHeight(120)
        layout.addWidget(allowed_label)
        layout.addWidget(self.allowed_paths_edit)

        self.auto_send_checkbox = QCheckBox(
            "Po otwarciu ChatGPT automatycznie wstaw i wyślij prompt startowy"
        )
        self.auto_send_checkbox.setObjectName("ProjectCreatorAutoSend")
        self.auto_send_checkbox.setChecked(True)
        layout.addWidget(self.auto_send_checkbox)

        self.confirm_checkbox = QCheckBox(
            "Zatwierdzam utworzenie/podłączenie repozytorium, przygotowanie workspace, Start, Re-arm i przekazanie prompta."
        )
        self.confirm_checkbox.setObjectName("ProjectCreatorConfirm")
        self.confirm_checkbox.toggled.connect(self._update_submit_state)
        layout.addWidget(self.confirm_checkbox)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Anuluj")
        cancel.clicked.connect(self.reject)
        self.submit_button = QPushButton("Utwórz i uruchom")
        self.submit_button.setObjectName("ProjectCreatorSubmit")
        self.submit_button.clicked.connect(self._submit)
        buttons.addWidget(cancel)
        buttons.addWidget(self.submit_button)
        layout.addLayout(buttons)

        self.feedback = QLabel("Nie wykonano jeszcze żadnej operacji.")
        self.feedback.setObjectName("ProjectCreatorFeedback")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

        self.name_edit.textChanged.connect(self._sync_alias)

    def _mode_changed(self, *_args: object) -> None:
        existing = self.mode_combo.currentData() == "existing"
        self.source_label.setVisible(existing)
        self.source_edit.setVisible(existing)
        self.visibility_label.setVisible(not existing)
        self.visibility_combo.setVisible(not existing)
        self.submit_button.setText("Podłącz i uruchom" if existing else "Utwórz i uruchom")

    def _sync_alias(self, value: str) -> None:
        if self.alias_edit.isModified():
            return
        normalized = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
        while "--" in normalized:
            normalized = normalized.replace("--", "-")
        self.alias_edit.setText(normalized[:32])

    def _browse_projects_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Wybierz katalog projektów",
            self.projects_root_edit.text() or str(Path.home()),
        )
        if selected:
            self.projects_root_edit.setText(selected)

    def _update_submit_state(self, *_args: object) -> None:
        self.submit_button.setEnabled(self.confirm_checkbox.isChecked())

    def _submit(self) -> None:
        if not self.confirm_checkbox.isChecked():
            return
        self.feedback.setText("Uruchamianie kompletnego planu projektu…")
        self.submit_button.setEnabled(False)
        self.submitted.emit(self.payload())
        self.accept()
