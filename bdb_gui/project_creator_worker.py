from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .project_creator import ProjectCreatorPlan, ProjectCreatorResult, ProjectCreatorService


@dataclass(frozen=True)
class ProjectCreatorOutcome:
    ok: bool
    plan: ProjectCreatorPlan | None = None
    result: ProjectCreatorResult | None = None
    error_code: str | None = None
    error_message: str | None = None


class ProjectCreatorWorkerSignals(QObject):
    completed = Signal(object)


class ProjectCreatorWorker(QRunnable):
    def __init__(
        self,
        service: ProjectCreatorService,
        *,
        workspaces_root: str,
        payload: dict[str, Any],
    ) -> None:
        super().__init__()
        self._service = service
        self._workspaces_root = workspaces_root
        self._payload = dict(payload)
        self.signals = ProjectCreatorWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        plan: ProjectCreatorPlan | None = None
        try:
            plan = self._service.build_plan(
                workspaces_root=self._workspaces_root,
                **self._payload,
            )
            result = self._service.execute(plan, workspaces_root=self._workspaces_root)
            outcome = ProjectCreatorOutcome(ok=result.ok, plan=plan, result=result)
        except (OSError, TypeError, ValueError) as error:
            outcome = ProjectCreatorOutcome(
                ok=False,
                plan=plan,
                error_code="project_creator_plan_invalid",
                error_message=f"{type(error).__name__}: {error}",
            )
        except Exception as error:
            outcome = ProjectCreatorOutcome(
                ok=False,
                plan=plan,
                error_code="project_creator_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(outcome)
