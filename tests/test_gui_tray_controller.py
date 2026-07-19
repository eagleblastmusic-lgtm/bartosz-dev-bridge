from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.tray import (  # noqa: E402
    NotificationMessage,
    TrayController,
    control_notification,
    export_notification,
    prepare_notification,
)


class FakeWindow(QObject):
    control_finished = Signal(object)
    dashboard_ready = Signal()
    prepare_finished = Signal(object)
    diagnostics_export_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.visible = True
        self.active = False
        self.stop_started = False
        self.force_closed = False

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:
        pass

    def isVisible(self) -> bool:
        return self.visible

    def has_active_task(self) -> bool:
        return self.active

    def request_confirmed_stop_for_exit(self) -> bool:
        self.stop_started = True
        return True

    def force_close(self) -> None:
        self.force_closed = True


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-bdb-tray"])


def test_notification_mapping_is_typed_and_bounded() -> None:
    success = control_notification(SimpleNamespace(action="start", ok=True, project_alias="alpha"))
    failure = control_notification(SimpleNamespace(action="stop", ok=False, project_alias="alpha", error_code="stop_failed"))
    prepared = prepare_notification(SimpleNamespace(ok=True, project_alias="alpha"))
    exported = export_notification(
        SimpleNamespace(ok=True, result=SimpleNamespace(output_path="C:/tmp/bdb.zip"))
    )

    assert success.level == "success" and "uruchomiony" in success.body
    assert failure.level == "error" and "stop_failed" in failure.body
    assert prepared.level == "success" and "alpha" in prepared.body
    assert exported.level == "success" and "bdb.zip" in exported.body


def test_close_hides_to_tray_without_quitting() -> None:
    app = application()
    window = FakeWindow()
    messages: list[NotificationMessage] = []
    controller = TrayController(
        app,
        window,
        available_override=True,
        notification_sink=messages.append,
        exit_choice_provider=lambda: "cancel",
    )
    event = QCloseEvent()

    handled = controller.handle_close(event)

    assert handled is True
    assert event.isAccepted() is False
    assert window.visible is False
    assert window.force_closed is False
    assert len(messages) == 1


def test_leave_exit_closes_panel_without_stop() -> None:
    app = application()
    window = FakeWindow()
    controller = TrayController(
        app,
        window,
        available_override=True,
        notification_sink=lambda message: None,
        exit_choice_provider=lambda: "leave",
    )

    controller.request_exit()

    assert window.stop_started is False
    assert window.force_closed is True


def test_stop_exit_waits_for_post_stop_refresh_to_be_idle() -> None:
    app = application()
    window = FakeWindow()
    controller = TrayController(
        app,
        window,
        available_override=True,
        notification_sink=lambda message: None,
        exit_choice_provider=lambda: "stop",
    )

    controller.request_exit()
    assert window.stop_started is True
    assert window.force_closed is False

    window.active = True
    window.control_finished.emit(SimpleNamespace(action="stop", ok=True, project_alias="alpha"))
    app.processEvents()
    assert window.force_closed is False

    window.active = False
    window.dashboard_ready.emit()
    app.processEvents()
    assert window.force_closed is True


def test_active_operation_blocks_exit() -> None:
    app = application()
    window = FakeWindow()
    window.active = True
    messages: list[NotificationMessage] = []
    controller = TrayController(
        app,
        window,
        available_override=True,
        notification_sink=messages.append,
        exit_choice_provider=lambda: "leave",
    )

    controller.request_exit()

    assert window.force_closed is False
    assert messages[-1].level == "warning"
