from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QTabWidget

from .project_window import ProjectControlCenterWindow
from .session_history import SessionHistoryService, SessionHistorySnapshot
from .session_history_view import PathOpener, SessionHistoryWidget
from .session_history_view_hardening import install_session_history_diagnostics
from .session_history_worker import SessionHistoryWorker
from .tray_window import TrayProjectControlCenterWindow


install_session_history_diagnostics(SessionHistoryWidget)


class SessionHistoryWindowMixin:
    """Install the read-only session/receipt view without rewriting the base window."""

    def __init__(
        self,
        *,
        session_history_service: SessionHistoryService | None = None,
        session_path_opener: PathOpener | None = None,
        **kwargs: Any,
    ) -> None:
        self._session_history_service = session_history_service or SessionHistoryService()
        self._session_path_opener = session_path_opener
        self._session_history_worker: SessionHistoryWorker | None = None
        self._last_session_history: SessionHistorySnapshot | None = None
        super().__init__(**kwargs)
        self._install_session_history_page()

    @property
    def last_session_history(self) -> SessionHistorySnapshot | None:
        return self._last_session_history

    def smoke_report(self) -> dict[str, Any]:
        report = super().smoke_report()
        report.update(self.session_history_view.smoke_report())
        report["history_tabs_present"] = self.history_tabs.count() == 2
        return report

    def _install_session_history_page(self) -> None:
        history_index = self.pages.indexOf(self.history_view)
        if history_index < 0:
            raise RuntimeError("History page is missing")
        self.pages.removeWidget(self.history_view)
        self.history_tabs = QTabWidget()
        self.history_tabs.setObjectName("HistoryTabs")
        self.session_history_view = SessionHistoryWidget(path_opener=self._session_path_opener)
        self.session_history_view.refresh_requested.connect(self._start_session_history_read)
        self.history_tabs.addTab(self.session_history_view, "Sesje i receipts")
        self.history_tabs.addTab(self.history_view, "Zdarzenia Journalu")
        self.pages.insertWidget(history_index, self.history_tabs)

    def _apply_selected_project_to_views(self) -> None:
        super()._apply_selected_project_to_views()
        if not hasattr(self, "session_history_view"):
            return
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            self.session_history_view.set_project_available(False)
            return
        self.session_history_view.set_project(self.project_selector.currentText(), workspace_root)

    def _set_project_available(self, available: bool) -> None:
        super()._set_project_available(available)
        if hasattr(self, "session_history_view"):
            self.session_history_view.set_project_available(available)

    def _reset_project_reads(self) -> None:
        super()._reset_project_reads()
        self._last_session_history = None

    def _has_active_task(self) -> bool:
        return super()._has_active_task() or self._session_history_worker is not None

    def _set_global_busy(self, busy: bool, message: str = "") -> None:
        super()._set_global_busy(busy, message)
        if hasattr(self, "session_history_view"):
            self.session_history_view.set_busy(busy, message)

    def _start_session_history_read(self, limit: int) -> None:
        if self._has_active_task():
            return
        workspace_root = self._selected_workspace_root()
        if workspace_root is None:
            return
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return
        self._set_global_busy(True, "Odczytywanie bounded historii zakończonych sesji…")
        worker = SessionHistoryWorker(
            self._session_history_service,
            workspace_root,
            limit=limit,
        )
        worker.signals.completed.connect(self._apply_session_history_snapshot)
        self._session_history_worker = worker
        self._thread_pool.start(worker)

    def _apply_session_history_snapshot(self, snapshot: SessionHistorySnapshot) -> None:
        self._session_history_worker = None
        self._last_session_history = snapshot
        self._set_global_busy(False)
        self.session_history_view.apply_snapshot(snapshot)
        self.status_line.setText(
            f"Historia sesji: {len(snapshot.sessions)} bounded podsumowań."
            if snapshot.ok
            else f"Historia sesji niedostępna: {snapshot.error_code or 'unknown'}"
        )


class SessionProjectControlCenterWindow(SessionHistoryWindowMixin, ProjectControlCenterWindow):
    pass


class SessionTrayProjectControlCenterWindow(SessionHistoryWindowMixin, TrayProjectControlCenterWindow):
    pass
