from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from PySide6.QtCore import QObject, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QStyle, QSystemTrayIcon


ExitChoice = Literal["leave", "stop", "cancel"]
ExitChoiceProvider = Callable[[], ExitChoice]


class TrayWindow(Protocol):
    control_finished: object
    diagnostics_export_finished: object
    prepare_finished: object

    def show(self) -> None: ...
    def hide(self) -> None: ...
    def raise_(self) -> None: ...
    def activateWindow(self) -> None: ...
    def isVisible(self) -> bool: ...
    def has_active_task(self) -> bool: ...
    def request_confirmed_stop_for_exit(self) -> bool: ...
    def force_close(self) -> None: ...


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str
    level: Literal["info", "success", "warning", "error"] = "info"


def control_notification(result: object) -> NotificationMessage:
    action = str(getattr(result, "action", "operation"))
    ok = bool(getattr(result, "ok", False))
    alias = getattr(result, "project_alias", None) or "wybrany projekt"
    if ok:
        labels = {"start": "uruchomiony", "stop": "zatrzymany", "rearm": "ponownie uzbrojony"}
        return NotificationMessage(
            "BDB Control Center",
            f"Projekt {alias}: {labels.get(action, 'operacja zakończona')}.",
            "success",
        )
    code = getattr(result, "error_code", None) or "operation_failed"
    return NotificationMessage("Operacja BDB nieudana", f"{alias}: {code}", "error")


def prepare_notification(result: object) -> NotificationMessage:
    ok = bool(getattr(result, "ok", False))
    alias = getattr(result, "project_alias", None)
    plan = getattr(result, "plan", None)
    alias = alias or getattr(plan, "alias", None) or "projekt"
    if ok:
        return NotificationMessage("Projekt BDB przygotowany", f"{alias}: Prepare zakończony.", "success")
    code = getattr(result, "error_code", None) or "prepare_failed"
    return NotificationMessage("Prepare nieudany", f"{alias}: {code}", "error")


def export_notification(outcome: object) -> NotificationMessage:
    ok = bool(getattr(outcome, "ok", False))
    result = getattr(outcome, "result", None)
    if ok and result is not None:
        path = getattr(result, "output_path", "pakiet ZIP")
        return NotificationMessage("Diagnostyka wyeksportowana", str(path), "success")
    code = getattr(outcome, "error_code", None) or "diagnostics_export_failed"
    return NotificationMessage("Eksport diagnostyczny nieudany", str(code), "error")


class TrayController(QObject):
    """Local, event-driven system tray. It never polls and never starts BDB."""

    def __init__(
        self,
        application: QApplication,
        window: TrayWindow,
        *,
        exit_choice_provider: ExitChoiceProvider | None = None,
        available_override: bool | None = None,
        notification_sink: Callable[[NotificationMessage], None] | None = None,
    ) -> None:
        super().__init__()
        self._application = application
        self._window = window
        self._exit_choice_provider = exit_choice_provider
        self._notification_sink = notification_sink
        self._quit_requested = False
        self._quit_after_stop = False
        self._hidden_notice_sent = False
        available = QSystemTrayIcon.isSystemTrayAvailable() if available_override is None else available_override
        self.available = bool(available)

        self.tray = QSystemTrayIcon(self._icon(), self)
        self.tray.setToolTip("BDB Control Center")
        self.menu = QMenu()
        self.show_action = QAction("Pokaż Control Center", self.menu)
        self.hide_action = QAction("Ukryj okno", self.menu)
        self.exit_action = QAction("Zakończ Control Center…", self.menu)
        self.show_action.triggered.connect(self.show_window)
        self.hide_action.triggered.connect(self.hide_window)
        self.exit_action.triggered.connect(self.request_exit)
        self.menu.addAction(self.show_action)
        self.menu.addAction(self.hide_action)
        self.menu.addSeparator()
        self.menu.addAction(self.exit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)

        window.control_finished.connect(self._on_control_finished)  # type: ignore[attr-defined]
        window.prepare_finished.connect(self._on_prepare_finished)  # type: ignore[attr-defined]
        window.diagnostics_export_finished.connect(self._on_export_finished)  # type: ignore[attr-defined]

    def start(self) -> None:
        if self.available:
            self.tray.show()

    def handle_close(self, event: QCloseEvent) -> bool:
        if self.available and not self._quit_requested:
            event.ignore()
            self.hide_window()
            if not self._hidden_notice_sent:
                self.notify(NotificationMessage("BDB Control Center", "Aplikacja działa w zasobniku systemowym."))
                self._hidden_notice_sent = True
            return True
        event.accept()
        return False

    @Slot()
    def show_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    @Slot()
    def hide_window(self) -> None:
        self._window.hide()

    @Slot()
    def request_exit(self) -> None:
        if self._window.has_active_task():
            self.notify(NotificationMessage("Operacja w toku", "Zakończenie jest zablokowane do końca bieżącej operacji.", "warning"))
            self.show_window()
            return
        choice = self._exit_choice()
        if choice == "cancel":
            return
        if choice == "stop":
            self._quit_after_stop = True
            if not self._window.request_confirmed_stop_for_exit():
                self._quit_after_stop = False
                self.notify(NotificationMessage("Nie można zatrzymać BDB", "Brak wybranego projektu albo trwa inna operacja.", "warning"))
                self.show_window()
            return
        self.complete_quit()

    def complete_quit(self) -> None:
        self._quit_requested = True
        self.tray.hide()
        self._window.force_close()
        self._application.quit()

    def notify(self, message: NotificationMessage) -> None:
        if self._notification_sink is not None:
            self._notification_sink(message)
            return
        if not self.available:
            return
        icon = {
            "warning": QSystemTrayIcon.MessageIcon.Warning,
            "error": QSystemTrayIcon.MessageIcon.Critical,
        }.get(message.level, QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage(message.title, message.body, icon, 5000)

    @Slot(object)
    def _on_control_finished(self, result: object) -> None:
        self.notify(control_notification(result))
        if self._quit_after_stop and getattr(result, "action", None) == "stop":
            self._quit_after_stop = False
            if bool(getattr(result, "ok", False)):
                self.complete_quit()
            else:
                self.show_window()

    @Slot(object)
    def _on_prepare_finished(self, result: object) -> None:
        self.notify(prepare_notification(result))

    @Slot(object)
    def _on_export_finished(self, outcome: object) -> None:
        self.notify(export_notification(outcome))

    @Slot(QSystemTrayIcon.ActivationReason)
    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()

    def _exit_choice(self) -> ExitChoice:
        if self._exit_choice_provider is not None:
            return self._exit_choice_provider()
        box = QMessageBox()
        box.setWindowTitle("Zakończyć BDB Control Center?")
        box.setText("Wybierz, co zrobić z lokalnym BDB przed zamknięciem panelu.")
        leave = box.addButton("Pozostaw BDB uruchomiony", QMessageBox.ButtonRole.AcceptRole)
        stop = box.addButton("Zatrzymaj wybrany projekt", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is leave:
            return "leave"
        if clicked is stop:
            return "stop"
        return "cancel"

    def _icon(self) -> QIcon:
        style = self._application.style()
        return style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
