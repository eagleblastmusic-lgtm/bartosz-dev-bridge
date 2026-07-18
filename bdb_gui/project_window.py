from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QMessageBox

from .main_window import ControlCenterWindow
from .project_workers import PlanOutcome, PlanWorker, PrepareWorker
from .projects import PreparePlan, PrepareResult, ProjectPrepareService
from .projects_view import ProjectsWidget


PrepareConfirmationProvider = Callable[[PreparePlan], bool]


class ProjectControlCenterWindow(ControlCenterWindow):
    """Control Center with the P11 Projects wizard installed at page index 1."""

    def __init__(
        self,
        *,
        project_prepare_service: ProjectPrepareService | None = None,
        prepare_confirmation_provider: PrepareConfirmationProvider | None = None,
        **kwargs: Any,
    ) -> None:
        self._project_prepare_service = project_prepare_service or ProjectPrepareService()
        self._prepare_confirmation_provider = prepare_confirmation_provider
        self._plan_worker: PlanWorker | None = None
        self._prepare_worker: PrepareWorker | None = None
        super().__init__(**kwargs)
        self._install_projects_page()

    def smoke_report(self) -> dict[str, Any]:
        report = super().smoke_report()
        report.update(self.projects_view.smoke_report())
        return report

    def _install_projects_page(self) -> None:
        previous = self.pages.widget(1)
        self.pages.removeWidget(previous)
        previous.deleteLater()
        self.projects_view = ProjectsWidget()
        self.projects_view.plan_requested.connect(self._start_prepare_plan)
        self.projects_view.prepare_requested.connect(self._request_prepare)
        self.pages.insertWidget(1, self.projects_view)

    def _has_active_task(self) -> bool:
        return (
            super()._has_active_task()
            or self._plan_worker is not None
            or self._prepare_worker is not None
        )

    def _set_global_busy(self, busy: bool, message: str = "") -> None:
        super()._set_global_busy(busy, message)
        if hasattr(self, "projects_view"):
            self.projects_view.set_busy(busy, message)

    def _start_prepare_plan(self, payload: object) -> None:
        if self._has_active_task() or not isinstance(payload, dict):
            return
        self._set_global_busy(True, "Walidowanie niemutującego planu Prepare…")
        worker = PlanWorker(
            self._project_prepare_service,
            self._workspaces_root,
            payload,
        )
        worker.signals.completed.connect(self._apply_plan_outcome)
        self._plan_worker = worker
        self._thread_pool.start(worker)

    def _apply_plan_outcome(self, outcome: PlanOutcome) -> None:
        self._plan_worker = None
        self._set_global_busy(False)
        if outcome.ok and outcome.plan is not None:
            self.projects_view.apply_plan(outcome.plan)
            self.status_line.setText(
                "Plan Prepare zweryfikowany lokalnie. Nie wykonano mutacji."
            )
        else:
            self.projects_view.apply_plan_error(
                outcome.error_code or "prepare_plan_invalid",
                outcome.error_message or "brak szczegółów",
            )
            self.status_line.setText("Plan Prepare jest nieprawidłowy; nie wykonano mutacji.")

    def _request_prepare(self, plan: object) -> None:
        if self._has_active_task() or not isinstance(plan, PreparePlan):
            return
        if not self._confirm_prepare(plan):
            self.status_line.setText("Prepare anulowany przed uruchomieniem preparera.")
            return
        self._set_global_busy(True, f"Przygotowywanie projektu {plan.alias}…")
        worker = PrepareWorker(self._project_prepare_service, plan)
        worker.signals.completed.connect(self._apply_prepare_result)
        self._prepare_worker = worker
        self._thread_pool.start(worker)

    def _apply_prepare_result(self, result: PrepareResult) -> None:
        self._prepare_worker = None
        self.projects_view.apply_prepare_result(result)
        self._mutation_operations_invoked += result.mutation_operations_invoked
        self._set_global_busy(False)
        if result.ok:
            self.status_line.setText(
                f"Projekt {result.project_alias or result.plan.alias} przygotowany. Odświeżam listę projektów."
            )
            self.start_bootstrap()
        else:
            self.status_line.setText(
                f"Prepare nieudany: {result.error_code or 'prepare_failed'} — "
                f"{result.error_message or 'brak szczegółów'}"
            )

    def _confirm_prepare(self, plan: PreparePlan) -> bool:
        if self._prepare_confirmation_provider is not None:
            return bool(self._prepare_confirmation_provider(plan))
        answer = QMessageBox.question(
            self,
            "Przygotować projekt BDB?",
            (
                f"Alias: {plan.alias}\n"
                f"Source repo: {plan.source_repo}\n"
                f"Workspace: {plan.workspace_root}\n"
                f"Allowed paths: {len(plan.allowed_paths)}\n\n"
                "Operacja utworzy lokalny control repo i workspace oraz zaktualizuje konfigurację Native Host. "
                "Preparer przerwie pracę, gdy source checkout jest brudny, detached albo wystąpi kolizja."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
