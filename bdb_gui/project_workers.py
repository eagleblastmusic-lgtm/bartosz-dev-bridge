from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .projects import PreparePlan, PrepareResult, ProjectPrepareService


@dataclass(frozen=True)
class WorkflowOutcome:
    key: str
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None


class WorkflowWorker(QRunnable):
    """Run the active vNext API using the existing Qt worker/signals pattern."""

    def __init__(self, key: str, operation: Callable[[], Any]) -> None:
        super().__init__()
        self.key = key
        self.operation = operation
        self.signals = PlanWorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            outcome = WorkflowOutcome(self.key, result=self.operation())
        except Exception as exc:
            import subprocess
            code = "command_timeout" if isinstance(exc, subprocess.TimeoutExpired) else getattr(exc, "code", "workflow_internal_error")
            outcome = WorkflowOutcome(self.key, error_code=code, error_message=str(exc))
        self.signals.completed.emit(outcome)


@dataclass(frozen=True)
class PlanOutcome:
    ok: bool
    plan: PreparePlan | None = None
    error_code: str | None = None
    error_message: str | None = None


class PlanWorkerSignals(QObject):
    completed = Signal(object)


class PlanWorker(QRunnable):
    def __init__(
        self,
        service: ProjectPrepareService,
        workspaces_root: str,
        payload: dict[str, Any],
    ) -> None:
        super().__init__()
        self._service = service
        self._workspaces_root = workspaces_root
        self._payload = dict(payload)
        self.signals = PlanWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            plan = self._service.build_plan(
                workspaces_root=self._workspaces_root,
                **self._payload,
            )
            outcome = PlanOutcome(ok=True, plan=plan)
        except (OSError, TypeError, ValueError) as error:
            outcome = PlanOutcome(
                ok=False,
                error_code="prepare_plan_invalid",
                error_message=f"{type(error).__name__}: {error}",
            )
        except Exception as error:
            outcome = PlanOutcome(
                ok=False,
                error_code="gui_prepare_plan_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(outcome)


class PrepareWorkerSignals(QObject):
    completed = Signal(object)


class PrepareWorker(QRunnable):
    def __init__(self, service: ProjectPrepareService, plan: PreparePlan) -> None:
        super().__init__()
        self._service = service
        self._plan = plan
        self.signals = PrepareWorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.execute(self._plan)
        except Exception as error:
            result = PrepareResult(
                plan=self._plan,
                ok=False,
                operation_id="gui-internal:prepare",
                project_alias=self._plan.alias,
                operator_data={},
                error_code="gui_prepare_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )
        self.signals.completed.emit(result)
