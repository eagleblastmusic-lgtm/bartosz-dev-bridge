"""Canonical AUTO projection and command boundary for Project Center.

The Project Center is a projection of canonical state.  This module keeps the
UI-facing selection separate from the durable scope cursor and exposes only
bounded commands to the canonical Project Memory v2 authority.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .auto_scope_contract import AutoScope, DEFAULT_AUTO_SCOPE
from .project_memory import (
    GATE_STATUS_VALUES,
    OPEN_QUESTION_STATUS_VALUES,
    ProjectMemoryError,
    ProjectMemoryStore,
    milestone_gate_id,
    milestone_gate_statuses,
    open_question_statuses,
    planning_gate_statuses,
    task_prerequisite_blockers,
    validate_prerequisite_state,
)
from .project_catalog import classify_dependency_targets
from .project_memory_v2_store import ProjectMemoryStoreV2, ProjectMemoryV2Error
from .scope_orchestrator import (
    CanonicalPlanGraph,
    PlanMilestoneNode,
    PlanPrerequisiteNode,
    PlanTaskNode,
    ScopeAction,
    ScopeOrchestrator,
)
from .stop_fence import execute_resume_transaction, execute_stop_transaction


PROJECT_CENTER_AUTO_UI_VERSION = "1.0.0"
AUTO_SCOPE_OPTIONS: tuple[AutoScope, ...] = (
    AutoScope.TASK,
    AutoScope.MILESTONE,
    AutoScope.PROJECT,
    AutoScope.UNTIL_STOPPED,
)

AUTO_STATUS_REASON_TEXT: dict[str, str] = {
    "READY": "AUTO jest gotowe do uruchomienia po potwierdzeniu wybranego scope.",
    "WAITING": "AUTO czeka na zakończenie trwałego oczekiwania.",
    "WAITING_FOR_PLAN": "Brak zatwierdzonego planu lub plan został wyczerpany.",
    "PAUSED": "AUTO jest wstrzymane do czasu rozstrzygnięcia checkpointu.",
    "BLOCKED": "AUTO jest zablokowane przez kanoniczny blocker.",
    "STOPPED": "AUTO jest zatrzymane przez kanoniczny STOP fence.",
    "CI_WAITING": "AUTO czeka na wynik CI zapisany w stanie kanonicznym.",
    "DELIVERY_UNCERTAIN": "Dostarczenie jest niepewne; wymagane jest kanoniczne uzgodnienie.",
    "OPERATOR_CHECKPOINT": "Wymagana jest decyzja operatora w kanonicznym checkpointcie.",
    "COMPLETED": "AUTO zakończyło dozwolony zakres.",
    "PROJECT_NOT_SELECTED": "Wybierz projekt, aby odczytać kanoniczny stan AUTO.",
    "AUTO_START_AVAILABLE": "Kanoniczny plan jest dostępny; start wymaga jawnego potwierdzenia.",
    "MILESTONE_GATE_PENDING": "Bieżący milestone oczekuje na jawne zatwierdzenie gate.",
    "MILESTONE_SCOPE_COMPLETED": "Bieżący milestone zakończono; następny wymaga jawnego startu.",
    "AUTO_STARTED_NEXT_MILESTONE": "Nowy milestone scope uruchomiono jawnie.",
    "PREREQUISITE_STATE_UNAVAILABLE": "Kanoniczny stan prerequisite jest niedostępny lub niejednoznaczny.",
}


@dataclass(frozen=True)
class AutoControlSpec:
    """Accessibility contract for controls rendered by Project Center."""

    control_id: str
    accessible_name: str
    keyboard_focusable: bool = True
    exposes_disabled_reason: bool = True


AUTO_UI_CONTROL_CONTRACT: tuple[AutoControlSpec, ...] = (
    AutoControlSpec("scope_selector", "AUTO scope"),
    AutoControlSpec("start", "Uruchom AUTO"),
    AutoControlSpec("stop", "STOP"),
    AutoControlSpec("continue", "Kontynuuj"),
    AutoControlSpec("resume", "Wznów"),
    AutoControlSpec("milestone_gate", "Zatwierdź milestone gate"),
    AutoControlSpec("planning_gate", "Zalicz gate"),
    AutoControlSpec("open_question", "Rozstrzygnij open question"),
)


@dataclass(frozen=True)
class CanonicalAutoState:
    """Read-only canonical state used to build the Project Center projection."""

    project_id: str = ""
    scope: AutoScope = DEFAULT_AUTO_SCOPE
    scope_epoch: int = 0
    run_id: str | None = None
    current_milestone_id: str | None = None
    current_task_id: str | None = None
    milestone_gate_id: str | None = None
    milestone_gate_status: str | None = None
    planning_gate_id: str | None = None
    planning_gate_status: str | None = None
    open_question_id: str | None = None
    open_question_status: str | None = None
    prerequisite_revision: int = 0
    prerequisite_error: str | None = None
    next_milestone_id: str | None = None
    scope_status: str = "WAITING_FOR_PLAN"
    continuation_status: str = "NONE"
    reentry_status: str = "NONE"
    reason_code: str = "WAITING_FOR_PLAN"
    reason: str = AUTO_STATUS_REASON_TEXT["WAITING_FOR_PLAN"]
    plan_available: bool = False
    plan_version: int | None = None
    canonical_revision: int = 0
    stop_fenced: bool = False
    p2_completed: bool = False
    p3_started: bool = False
    authority: str = "ProjectMemoryStoreV2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", AutoScope(self.scope))
        if self.scope_epoch < 0 or self.canonical_revision < 0 or self.prerequisite_revision < 0:
            raise ValueError("canonical AUTO counters must be non-negative")
        if not self.reason:
            object.__setattr__(
                self,
                "reason",
                AUTO_STATUS_REASON_TEXT.get(self.scope_status, self.scope_status),
            )

    @property
    def premium_p2_completed(self) -> bool:
        return self.p2_completed

    @property
    def premium_p3_started(self) -> bool:
        return self.p3_started

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "scope": self.scope.value,
            "scope_epoch": self.scope_epoch,
            "run_id": self.run_id,
            "current_milestone_id": self.current_milestone_id,
            "current_task_id": self.current_task_id,
            "milestone_gate_id": self.milestone_gate_id,
            "milestone_gate_status": self.milestone_gate_status,
            "planning_gate_id": self.planning_gate_id,
            "planning_gate_status": self.planning_gate_status,
            "open_question_id": self.open_question_id,
            "open_question_status": self.open_question_status,
            "prerequisite_revision": self.prerequisite_revision,
            "prerequisite_error": self.prerequisite_error,
            "next_milestone_id": self.next_milestone_id,
            "scope_status": self.scope_status,
            "continuation_status": self.continuation_status,
            "reentry_status": self.reentry_status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "plan_available": self.plan_available,
            "plan_version": self.plan_version,
            "canonical_revision": self.canonical_revision,
            "stop_fenced": self.stop_fenced,
            "p2_completed": self.p2_completed,
            "p3_started": self.p3_started,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class ProjectCenterAutoViewModel:
    """Deterministic GUI projection; ``selected_scope`` is UI-only state."""

    canonical: CanonicalAutoState
    selected_scope: AutoScope = DEFAULT_AUTO_SCOPE
    scope_options: tuple[AutoScope, ...] = AUTO_SCOPE_OPTIONS

    @classmethod
    def from_canonical(
        cls,
        canonical: CanonicalAutoState,
        *,
        selected_scope: AutoScope | str | None = None,
        browser_local_state: Mapping[str, Any] | None = None,
    ) -> "ProjectCenterAutoViewModel":
        # Browser/local state is intentionally accepted only as an ignored
        # observation.  It cannot select a scope or change a command.
        del browser_local_state
        selected = AutoScope(selected_scope) if selected_scope is not None else DEFAULT_AUTO_SCOPE
        return cls(canonical=canonical, selected_scope=selected)

    def select_scope(self, scope: AutoScope | str) -> "ProjectCenterAutoViewModel":
        """Return a new projection without mutating canonical authority."""
        return ProjectCenterAutoViewModel.from_canonical(self.canonical, selected_scope=scope)

    @property
    def current_milestone(self) -> str:
        return self.canonical.current_milestone_id or "—"

    @property
    def current_task(self) -> str:
        return self.canonical.current_task_id or "—"

    @property
    def scope_status(self) -> str:
        return self.canonical.scope_status

    @property
    def continuation_status(self) -> str:
        return self.canonical.continuation_status

    @property
    def reentry_status(self) -> str:
        return self.canonical.reentry_status

    @property
    def blocker_reason(self) -> str:
        return self.canonical.reason or AUTO_STATUS_REASON_TEXT.get(self.scope_status, self.scope_status)

    @property
    def selected_scope_is_pending(self) -> bool:
        return self.selected_scope != self.canonical.scope

    @property
    def can_start(self) -> bool:
        return bool(
            self.canonical.plan_available
            and (
                self.scope_status in {"READY", "AUTO_START_AVAILABLE"}
                or (
                    self.scope_status == "COMPLETED"
                    and self.canonical.scope == AutoScope.MILESTONE
                    and self.canonical.next_milestone_id is not None
                )
            )
            and not self.canonical.stop_fenced
        )

    @property
    def can_stop(self) -> bool:
        return self.scope_status in {
            "ACTIVE",
            "RUNNABLE",
            "WAITING",
            "PAUSED",
            "CI_WAITING",
            "DELIVERY_UNCERTAIN",
            "OPERATOR_CHECKPOINT",
        } and not self.canonical.stop_fenced

    @property
    def can_continue(self) -> bool:
        return self.scope_status in {
            "ACTIVE",
            "RUNNABLE",
            "WAITING",
            "PAUSED",
            "CI_WAITING",
            "DELIVERY_UNCERTAIN",
            "OPERATOR_CHECKPOINT",
        } and not self.canonical.stop_fenced

    @property
    def can_resume(self) -> bool:
        return self.scope_status in {
            "STOPPED",
            "PAUSED",
            "DELIVERY_UNCERTAIN",
            "OPERATOR_CHECKPOINT",
        } or self.reentry_status in {"PENDING", "OPERATOR_CHECKPOINT", "DELIVERY_UNCERTAIN"}

    def disabled_reason(self, action: str) -> str:
        enabled = {
            "start": self.can_start,
            "stop": self.can_stop,
            "continue": self.can_continue,
            "resume": self.can_resume,
        }.get(action)
        if enabled is None:
            raise ValueError(f"unknown AUTO action: {action}")
        if enabled:
            return ""
        return self.blocker_reason or AUTO_STATUS_REASON_TEXT.get(self.scope_status, self.scope_status)

    def start_intent(self) -> dict[str, Any]:
        return {
            "command": "START_AUTO",
            "project_id": self.canonical.project_id,
            "scope": self.selected_scope.value,
            "explicit_confirmation_required": True,
        }

    def continue_intent(self) -> dict[str, Any]:
        # The orchestrator selects the next task/milestone.  The GUI sends no
        # task or milestone suggestion.
        return {"command": "CONTINUE_AUTO", "project_id": self.canonical.project_id}

    def resume_intent(self) -> dict[str, Any]:
        return {"command": "RESUME_AUTO", "project_id": self.canonical.project_id}

    def stop_intent(self) -> dict[str, Any]:
        return {"command": "STOP_AUTO", "project_id": self.canonical.project_id}


class ProjectCenterAutoCommandError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AutoCommandReceipt:
    command: str
    project_id: str
    accepted: bool
    reason_code: str
    explanation: str
    scope: AutoScope | None = None
    scope_epoch: int | None = None
    current_milestone_id: str | None = None
    current_task_id: str | None = None
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "project_id": self.project_id,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "scope": self.scope.value if self.scope else None,
            "scope_epoch": self.scope_epoch,
            "current_milestone_id": self.current_milestone_id,
            "current_task_id": self.current_task_id,
            "idempotent": self.idempotent,
        }


class ProjectCenterAutoCommands(Protocol):
    def snapshot(self, *, plan_available: bool = False, plan_version: str | None = None) -> CanonicalAutoState:
        ...

    def start_auto(self, scope: AutoScope, *, confirmed: bool) -> AutoCommandReceipt:
        ...

    def continue_auto(self) -> AutoCommandReceipt:
        ...

    def resume_auto(self) -> AutoCommandReceipt:
        ...

    def stop_auto(self) -> AutoCommandReceipt:
        ...

    def pass_milestone_gate(self, gate_id: str, *, expected_revision: int | None = None) -> AutoCommandReceipt:
        ...

    def pass_gate(self, gate_id: str, *, expected_revision: int | None = None) -> AutoCommandReceipt:
        ...

    def resolve_open_question(self, question_id: str, *, expected_revision: int | None = None) -> AutoCommandReceipt:
        ...


class CanonicalProjectCenterAutoCommands:
    """Adapter that routes Project Center commands to Project Memory v2."""

    STORAGE_DATABASE = "control/project-memory-v2/<project_id>.db"
    SCHEMA_OWNER = "ProjectMemoryStoreV2"
    TRANSACTION_AUTHORITY = "ProjectMemoryStoreV2._transaction"
    STOP_COMMAND_AUTHORITY = "ProjectMemoryStoreV2.request_stop"
    RESUME_COMMAND_AUTHORITY = "ProjectMemoryStoreV2.resume_scope"
    SECOND_AUTHORITY_CREATED = False

    def __init__(
        self,
        runtime_root: str | Path,
        project_id: str,
        *,
        project_provider: Callable[[], Any] | None = None,
        plan_provider: Callable[[], Any | None] | None = None,
        memory_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).expanduser().absolute()
        self.project_id = project_id
        self._project_provider = project_provider
        self._plan_provider = plan_provider
        self._memory_provider = memory_provider

    @property
    def db_path(self) -> Path:
        return self.runtime_root / "control" / "project-memory-v2" / f"{self.project_id}.db"

    def _plan(self) -> Any | None:
        return self._plan_provider() if self._plan_provider is not None else None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _memory_value(self) -> Any:
        value = self._memory_provider() if self._memory_provider is not None else ProjectMemoryStore(self.runtime_root, self.project_id)
        if hasattr(value, "read_state"):
            value = value.read_state()
        return value

    @staticmethod
    def _memory_execution(value: Any) -> tuple[Mapping[str, Any], int]:
        if hasattr(value, "execution"):
            execution = value.execution
            revision = int(getattr(value, "revision", 0))
        elif isinstance(value, Mapping):
            execution = value.get("execution", {})
            revision = int(value.get("revision", 0) or 0)
        else:
            execution, revision = {}, 0
        return execution if isinstance(execution, Mapping) else {}, revision

    def _ensure_project_memory_plan(self, plan: Any) -> None:
        """Ensure the existing v1 Project Memory has durable gate projections."""
        value = self._memory_provider() if self._memory_provider is not None else ProjectMemoryStore(self.runtime_root, self.project_id)
        ensure = getattr(value, "ensure_initial_plan", None)
        if not callable(ensure):
            return
        try:
            ensure(plan)
        except ProjectMemoryError as exc:
            if exc.code != "plan_already_exists":
                raise ProjectCenterAutoCommandError(exc.code, str(exc)) from exc

    def _prerequisite_projection(
        self,
        plan: Any | None,
        current_milestone_id: str | None,
        current_task_id: str | None = None,
    ) -> dict[str, Any]:
        if plan is None or not plan.milestones:
            return {}
        value = self._memory_value()
        execution, revision = self._memory_execution(value)
        durable_milestone_gates = milestone_gate_statuses(plan, value)
        # Validate every durable prerequisite namespace before projecting any
        # operator action.  Missing maps remain safe legacy defaults; present
        # malformed maps are unavailable rather than silently repaired.
        validate_prerequisite_state(plan, value)

        milestone_ids = [item.milestone_id for item in plan.milestones]
        current_id = current_milestone_id or milestone_ids[0]
        if current_id not in milestone_ids:
            raise ProjectMemoryError("milestone_gate_state_invalid", "current milestone is not present in the canonical plan")
        current_gate = milestone_gate_id(current_id)
        planning_gate_id: str | None = None
        planning_gate_status: str | None = None
        open_question_id: str | None = None
        open_question_status: str | None = None
        if hasattr(value, "execution"):
            raw_task_statuses = execution.get("task_statuses", {})
            if current_task_id is not None:
                current_task = next((task for task in plan.tasks if task.task_id == current_task_id), None)
                if current_task is None or current_task.milestone_id != current_id:
                    raise ProjectMemoryError(
                        "prerequisite_state_invalid",
                        "current AUTO task is not present in the current canonical milestone",
                    )
                candidate_tasks = (current_task,)
            else:
                candidate_tasks = tuple(
                    task for task in plan.tasks if task.milestone_id == current_id
                )

            for task in candidate_tasks:
                task_status = str(raw_task_statuses.get(task.task_id, task.status)).lower() if isinstance(raw_task_statuses, Mapping) else task.status
                if task_status in {"completed", "skipped"}:
                    continue
                for blocker in task_prerequisite_blockers(plan, value, task):
                    if blocker["kind"] == "gate" and planning_gate_id is None:
                        planning_gate_id = blocker["id"]
                        planning_gate_status = blocker["status"]
                    elif blocker["kind"] == "open_question" and open_question_id is None:
                        open_question_id = blocker["id"]
                        open_question_status = blocker["status"]

        current_index = milestone_ids.index(current_id)
        next_milestone_id = milestone_ids[current_index + 1] if current_index + 1 < len(milestone_ids) else None
        return {
            "milestone_gate_id": current_gate,
            "milestone_gate_status": durable_milestone_gates[current_gate],
            "planning_gate_id": planning_gate_id,
            "planning_gate_status": planning_gate_status,
            "open_question_id": open_question_id,
            "open_question_status": open_question_status,
            "prerequisite_revision": revision,
            "prerequisite_error": None,
            "next_milestone_id": next_milestone_id,
        }

    def _safe_prerequisite_projection(
        self,
        plan: Any | None,
        current_milestone_id: str | None,
        current_task_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self._prerequisite_projection(plan, current_milestone_id, current_task_id)
        except (ProjectMemoryError, ValueError, TypeError) as exc:
            return {"prerequisite_error": getattr(exc, "code", "prerequisite_state_unavailable")}

    def _project_memory_task_blocker(self, task_id: str | None) -> tuple[str | None, str | None]:
        """Project a terminal v1 task failure without mutating the AUTO cursor.

        ProjectExecution records task results in the existing Project Memory
        execution document. AUTO continuation already treats that v1 task
        status as the logical authority; the read-only Project Center snapshot
        must observe the same blocker without a mutating Continue call.
        """
        if not task_id:
            return None, None
        try:
            execution, _revision = self._memory_execution(self._memory_value())
        except (ProjectMemoryError, OSError, ValueError, TypeError):
            return None, None
        raw_statuses = execution.get("task_statuses", {})
        if not isinstance(raw_statuses, Mapping) or str(raw_statuses.get(task_id, "")).lower() != "blocked":
            return None, None

        reason_code = "BLOCKED"
        explanation = AUTO_STATUS_REASON_TEXT["BLOCKED"]
        attempts = execution.get("attempts", [])
        if isinstance(attempts, list):
            for raw_attempt in reversed(attempts):
                if not isinstance(raw_attempt, Mapping) or str(raw_attempt.get("task_id")) != task_id:
                    continue
                if str(raw_attempt.get("result_status", "")).upper() == "STALE_RESULT":
                    continue
                failure_code = raw_attempt.get("failure_code")
                result_summary = raw_attempt.get("result_summary")
                if failure_code:
                    reason_code = str(failure_code)
                if result_summary:
                    explanation = str(result_summary)
                break
        return reason_code, explanation

    def snapshot(
        self,
        *,
        plan_available: bool = False,
        plan_version: str | None = None,
    ) -> CanonicalAutoState:
        """Read canonical state without creating a database or a cursor."""
        db_path = self.db_path
        try:
            plan = self._plan()
        except Exception:
            plan = None
        if not db_path.is_file():
            status = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
            reason_code = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
            return CanonicalAutoState(
                project_id=self.project_id,
                scope=DEFAULT_AUTO_SCOPE,
                scope_status=status,
                reason_code=reason_code,
                reason=AUTO_STATUS_REASON_TEXT[reason_code],
                plan_available=plan_available,
                plan_version=int(plan_version) if plan_version and str(plan_version).isdigit() else None,
                **self._safe_prerequisite_projection(plan, None),
            )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            project_row = (
                conn.execute(
                    "SELECT revision FROM projects WHERE project_id = ?",
                    (self.project_id,),
                ).fetchone()
                if "projects" in tables
                else None
            )
            cursor = (
                conn.execute(
                    "SELECT * FROM scope_cursors WHERE project_id = ?",
                    (self.project_id,),
                ).fetchone()
                if "scope_cursors" in tables
                else None
            )
            if cursor is None:
                status = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
                code = "AUTO_START_AVAILABLE" if plan_available else "WAITING_FOR_PLAN"
                return CanonicalAutoState(
                    project_id=self.project_id,
                    scope=DEFAULT_AUTO_SCOPE,
                    scope_status=status,
                    reason_code=code,
                    reason=AUTO_STATUS_REASON_TEXT[code],
                    plan_available=plan_available,
                    plan_version=int(plan_version) if plan_version and str(plan_version).isdigit() else None,
                    canonical_revision=int(project_row[0]) if project_row else 0,
                    **self._safe_prerequisite_projection(plan, None),
                )

            scope = AutoScope(cursor["scope"])
            raw_status = str(cursor["status"] if "status" in cursor.keys() else "ACTIVE")
            disposition = str(cursor["disposition"] or "ACTIVE")
            task_status = None
            if "task_execution_states" in tables and cursor["current_task_id"]:
                task_row = conn.execute(
                    "SELECT status FROM task_execution_states WHERE project_id = ? AND task_id = ?",
                    (self.project_id, cursor["current_task_id"]),
                ).fetchone()
                task_status = str(task_row[0]).upper() if task_row else None

            project_memory_reason_code, project_memory_explanation = self._project_memory_task_blocker(
                cursor["current_task_id"]
            )
            if project_memory_reason_code is not None:
                task_status = "BLOCKED"

            send_status = None
            if "send_intents" in tables:
                row = conn.execute(
                    "SELECT status FROM send_intents WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (self.project_id,),
                ).fetchone()
                send_status = str(row[0]) if row else None

            reentry_status = "NONE"
            if "session_reentries" in tables:
                row = conn.execute(
                    "SELECT liveness_state FROM session_reentries WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (self.project_id,),
                ).fetchone()
                reentry_status = str(row[0]) if row else "NONE"

            stop_fenced = raw_status == "STOPPED" or disposition == "STOPPED"
            if stop_fenced:
                status = "STOPPED"
                reason_code = "STOPPED"
            elif raw_status == "BLOCKED" or disposition in {"BLOCKED", "HALT_BLOCKED"}:
                status = "BLOCKED"
                reason_code = "BLOCKED"
            elif send_status == "UNCERTAIN":
                status = "DELIVERY_UNCERTAIN"
                reason_code = "DELIVERY_UNCERTAIN"
            elif reentry_status == "OPERATOR_CHECKPOINT":
                status = "OPERATOR_CHECKPOINT"
                reason_code = "OPERATOR_CHECKPOINT"
            elif disposition == "WAITING_FOR_PLAN":
                status = "WAITING_FOR_PLAN"
                reason_code = "WAITING_FOR_PLAN"
            elif task_status == "BLOCKED":
                status = "BLOCKED"
                reason_code = "BLOCKED"
            elif disposition in {"PAUSED", "PAUSE_MANUAL_GATE_REQUIRED", "PAUSE_POLICY_APPROVAL_REQUIRED"}:
                status = "PAUSED"
                reason_code = "PAUSED"
            elif disposition in {"WAIT_CI_WAITING", "CI_WAITING"}:
                status = "CI_WAITING"
                reason_code = "CI_WAITING"
            elif disposition in {"WAITING", "WAIT_DEPENDENCY_PENDING", "WAIT_MILESTONE_GATE_PENDING"}:
                status = "WAITING"
                reason_code = "WAITING"
            elif disposition in {"COMPLETED", "STOP_SCOPE_COMPLETE", "STOP_PROJECT_COMPLETE"}:
                status = "COMPLETED"
                reason_code = "COMPLETED"
            else:
                status = "ACTIVE"
                reason_code = "ACTIVE"

            persisted_reason_code: str | None = None
            persisted_explanation: str | None = None
            try:
                persisted = json.loads(cursor["explanation_json"] or "{}")
            except (TypeError, ValueError):
                persisted = {}
            if isinstance(persisted, Mapping):
                raw_reason_code = persisted.get("reason_code")
                raw_explanation = persisted.get("explanation")
                if raw_reason_code:
                    persisted_reason_code = str(raw_reason_code)
                if raw_explanation:
                    persisted_explanation = str(raw_explanation)
            project_memory_blocked = project_memory_reason_code is not None and status == "BLOCKED"
            if project_memory_blocked:
                reason_code = project_memory_reason_code
            elif persisted_reason_code:
                reason_code = persisted_reason_code

            continuation_status = send_status or "NONE"
            reason = (
                project_memory_explanation
                if project_memory_blocked and project_memory_explanation
                else persisted_explanation or AUTO_STATUS_REASON_TEXT.get(reason_code, f"Kanoniczny status: {reason_code}.")
            )
            return CanonicalAutoState(
                project_id=self.project_id,
                scope=scope,
                scope_epoch=int(cursor["scope_epoch"]),
                run_id=cursor["run_id"],
                current_milestone_id=cursor["current_milestone_id"],
                current_task_id=cursor["current_task_id"],
                scope_status=status,
                continuation_status=continuation_status,
                reentry_status=reentry_status,
                reason_code=reason_code,
                reason=reason,
                plan_available=plan_available,
                plan_version=int(cursor["plan_version"] or 0) or None,
                canonical_revision=int(cursor["state_revision"] or (project_row[0] if project_row else 0)),
                stop_fenced=stop_fenced,
                **self._safe_prerequisite_projection(
                    plan,
                    cursor["current_milestone_id"],
                    cursor["current_task_id"],
                ),
            )
        finally:
            conn.close()

    def _store_for_write(self) -> ProjectMemoryStoreV2:
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        project = self._project_provider() if self._project_provider is not None else None
        if project is None:
            raise ProjectCenterAutoCommandError("project_not_available", "canonical project record is unavailable")
        brief = project.brief.to_dict() if hasattr(project.brief, "to_dict") else dict(project.brief)
        store.ensure_project(
            project.display_name,
            project.repo_alias,
            project.local_repo_path,
            brief,
        )
        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        self._ensure_project_memory_plan(plan)
        try:
            store.ensure_initial_plan(plan)
        except ProjectMemoryV2Error as exc:
            if exc.code != "plan_already_exists":
                raise ProjectCenterAutoCommandError(exc.code, str(exc)) from exc
        return store

    @staticmethod
    def _plan_graph(plan: Any) -> CanonicalPlanGraph:
        milestone_gate_ids = {milestone_gate_id(item.milestone_id) for item in plan.milestones}
        context = plan.planning_context or {}
        planning_gate_ids = {item["id"] for item in context.get("gates", [])}
        open_question_ids = {item["id"] for item in context.get("open_questions", [])}
        if milestone_gate_ids & planning_gate_ids:
            collision = sorted(milestone_gate_ids & planning_gate_ids)[0]
            raise ProjectCenterAutoCommandError(
                "ambiguous_gate_mapping",
                f"planning gate {collision} collides with a milestone gate; no implicit mapping is allowed",
            )
        if planning_gate_ids & open_question_ids:
            collision = sorted(planning_gate_ids & open_question_ids)[0]
            raise ProjectCenterAutoCommandError(
                "ambiguous_prerequisite_mapping",
                f"planning gate and open question share the identifier {collision}",
            )
        milestones = tuple(
            PlanMilestoneNode(
                milestone_id=item.milestone_id,
                gate_id=milestone_gate_id(item.milestone_id),
                task_ids=tuple(task.task_id for task in plan.tasks if task.milestone_id == item.milestone_id),
            )
            for item in plan.milestones
        )
        tasks = tuple(
            PlanTaskNode(
                task_id=item.task_id,
                milestone_id=item.milestone_id,
                dependencies=tuple(item.dependencies),
            )
            for item in plan.tasks
        )
        dependency_targets = classify_dependency_targets(plan)
        referenced_prerequisites = {
            dependency
            for item in plan.tasks
            for dependency in item.dependencies
            if dependency_targets.get(dependency) in {"gate", "open_question"}
        }
        prerequisite_nodes = tuple(
            PlanPrerequisiteNode(prerequisite_id=identifier, kind=dependency_targets[identifier])
            for identifier in sorted(referenced_prerequisites)
        )
        return CanonicalPlanGraph(
            plan_identity=f"{plan.project_id}:plan:v{plan.plan_version}",
            plan_version=int(str(plan.plan_version).split(".", 1)[0]),
            milestones=milestones,
            tasks=tasks,
            prerequisites=prerequisite_nodes,
        )

    @staticmethod
    def _plan_statuses(plan: Any) -> dict[str, str]:
        mapping = {
            "completed": "ACCEPTED",
            "skipped": "ACCEPTED",
            "active": "IN_PROGRESS",
            "review": "IN_PROGRESS",
            "blocked": "BLOCKED",
            "pending": "NOT_STARTED",
        }
        return {item.task_id: mapping.get(item.status, "NOT_STARTED") for item in plan.tasks}

    @staticmethod
    def _orchestrator_task_status(value: object) -> str:
        return {
            "completed": "ACCEPTED",
            "skipped": "ACCEPTED",
            "active": "IN_PROGRESS",
            "review": "IN_PROGRESS",
            "blocked": "BLOCKED",
            "pending": "NOT_STARTED",
        }.get(str(value).lower(), str(value).upper())

    def _canonical_prerequisite_inputs(
        self,
        plan: Any,
        conn: sqlite3.Connection,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """Read task, planning prerequisite, and milestone gate status canonically."""

        statuses = self._plan_statuses(plan)
        prerequisite_statuses: dict[str, str] = {}

        memory_value = self._memory_value()
        execution, _revision = self._memory_execution(memory_value)
        durable_gate_statuses = milestone_gate_statuses(plan, memory_value)
        validate_prerequisite_state(plan, memory_value)

        raw_task_statuses = execution.get("task_statuses", {})
        if isinstance(raw_task_statuses, Mapping):
            for task_id, raw_status in raw_task_statuses.items():
                if str(task_id) in statuses:
                    statuses[str(task_id)] = self._orchestrator_task_status(raw_status)

        # V2 task rows are retained as a migration-compatible fallback for
        # callers that have not yet projected the task into v1 Project Memory.
        # A v1 status always wins because it is the existing logical authority.
        v2_rows = conn.execute(
            "SELECT task_id, status FROM task_execution_states WHERE project_id = ?",
            (self.project_id,),
        ).fetchall()
        for task_id, raw_status in v2_rows:
            if str(task_id) in statuses and (not isinstance(raw_task_statuses, Mapping) or task_id not in raw_task_statuses):
                statuses[str(task_id)] = self._orchestrator_task_status(raw_status)

        dependency_targets = classify_dependency_targets(plan)
        raw_gates = execution.get("gate_statuses", {})
        raw_questions = execution.get("open_question_statuses", {})
        for identifier, kind in dependency_targets.items():
            if kind == "gate":
                raw_status = raw_gates.get(identifier, "pending") if isinstance(raw_gates, Mapping) else "pending"
                prerequisite_statuses[identifier] = (
                    str(raw_status) if str(raw_status) in GATE_STATUS_VALUES else "pending"
                )
            elif kind == "open_question":
                raw_status = raw_questions.get(identifier, "open") if isinstance(raw_questions, Mapping) else "open"
                prerequisite_statuses[identifier] = (
                    str(raw_status) if str(raw_status) in OPEN_QUESTION_STATUS_VALUES else "open"
                )

        milestone_gate_inputs = {
            identifier: "ACCEPTED" if status == "passed" else "NOT_REACHED"
            for identifier, status in durable_gate_statuses.items()
        }
        return statuses, prerequisite_statuses, milestone_gate_inputs

    def _receipt_from_state(
        self,
        command: str,
        state: CanonicalAutoState,
        *,
        reason_code: str | None = None,
        explanation: str | None = None,
        idempotent: bool = False,
    ) -> AutoCommandReceipt:
        return AutoCommandReceipt(
            command=command,
            project_id=self.project_id,
            accepted=True,
            reason_code=reason_code or state.reason_code,
            explanation=explanation or state.reason,
            scope=state.scope,
            scope_epoch=state.scope_epoch,
            current_milestone_id=state.current_milestone_id,
            current_task_id=state.current_task_id,
            idempotent=idempotent,
        )

    @staticmethod
    def _plan_version_number(plan: Any) -> int:
        return int(str(plan.plan_version).split(".", 1)[0])

    @staticmethod
    def _next_milestone_id(plan: Any, current_milestone_id: str | None) -> str | None:
        milestone_ids = [item.milestone_id for item in plan.milestones]
        if not milestone_ids:
            return None
        if current_milestone_id is None:
            return milestone_ids[0]
        try:
            index = milestone_ids.index(current_milestone_id)
        except ValueError:
            return None
        return milestone_ids[index + 1] if index + 1 < len(milestone_ids) else None

    @staticmethod
    def _scope_history_id(run_id: str) -> str:
        return f"scope:{run_id}"

    def _ensure_scope_history(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        scope: AutoScope,
        milestone_id: str,
    ) -> None:
        """Create or validate the v2 history rows for the canonical run.

        A PROJECT/UNTIL_STOPPED run is one continuous scope that may cross
        milestone boundaries.  The v2 ``runs.milestone_id`` value therefore
        records the run's origin, while ``scopes.milestone_id`` is the current
        projection updated by ``_update_scope_history``.  TASK and MILESTONE
        remain bound to their originating milestone.
        """
        run = conn.execute("SELECT project_id, milestone_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            now = self._now_iso()
            conn.execute(
                "INSERT INTO runs (run_id, project_id, milestone_id, status, current_task_id, started_at, finished_at) VALUES (?, ?, ?, 'running', NULL, ?, NULL)",
                (run_id, self.project_id, milestone_id, now),
            )
        elif str(run[0]) != self.project_id or (
            scope not in (AutoScope.PROJECT, AutoScope.UNTIL_STOPPED)
            and str(run[1]) != milestone_id
        ):
            raise ProjectCenterAutoCommandError(
                "run_identity_conflict",
                f"run {run_id} is bound to a different project or milestone",
            )

        scope_id = self._scope_history_id(run_id)
        scope_row = conn.execute("SELECT project_id, milestone_id, mode FROM scopes WHERE scope_id = ?", (scope_id,)).fetchone()
        if scope_row is None:
            now = self._now_iso()
            conn.execute(
                "INSERT INTO scopes (scope_id, project_id, mode, status, milestone_id, max_tasks, started_at, finished_at) VALUES (?, ?, ?, 'RUNNING', ?, NULL, ?, NULL)",
                (scope_id, self.project_id, scope.value, milestone_id, now),
            )
        elif str(scope_row[0]) != self.project_id or str(scope_row[2]) != scope.value or (
            scope not in (AutoScope.PROJECT, AutoScope.UNTIL_STOPPED)
            and str(scope_row[1]) != milestone_id
        ):
            raise ProjectCenterAutoCommandError(
                "scope_identity_conflict",
                f"scope history {scope_id} is bound to a different canonical identity",
            )

    def _update_scope_history(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        scope: AutoScope,
        milestone_id: str,
        current_task_id: str | None,
        lifecycle_status: str,
    ) -> None:
        self._ensure_scope_history(conn, run_id=run_id, scope=scope, milestone_id=milestone_id)
        finished_at = self._now_iso() if lifecycle_status in {"completed", "failed", "stopped"} else None
        conn.execute(
            "UPDATE runs SET status = ?, current_task_id = ?, finished_at = ? WHERE run_id = ? AND project_id = ?",
            (lifecycle_status, current_task_id, finished_at, run_id, self.project_id),
        )
        scope_status = {
            "running": "RUNNING",
            "completed": "COMPLETED",
            "failed": "FAILED",
            "stopped": "STOPPED",
        }[lifecycle_status]
        conn.execute(
            "UPDATE scopes SET status = ?, milestone_id = ?, finished_at = ? WHERE scope_id = ? AND project_id = ?",
            (scope_status, milestone_id, finished_at, self._scope_history_id(run_id), self.project_id),
        )

    def start_auto(self, scope: AutoScope, *, confirmed: bool) -> AutoCommandReceipt:
        if not confirmed:
            raise ProjectCenterAutoCommandError(
                "explicit_confirmation_required",
                "AUTO start requires explicit confirmation of the selected scope",
            )
        try:
            requested_scope = AutoScope(scope)
        except ValueError as exc:
            raise ProjectCenterAutoCommandError("invalid_scope", "AUTO scope is invalid") from exc

        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        # Constructing the graph validates the two prerequisite namespaces
        # before a new run can be created.
        self._plan_graph(plan)
        if not plan.milestones:
            raise ProjectCenterAutoCommandError("plan_milestones_required", "AUTO requires at least one canonical milestone")
        self._ensure_project_memory_plan(plan)
        try:
            # Validate durable prerequisite identity before creating a v2
            # cursor; malformed state must not start a new run.
            self._prerequisite_projection(plan, None)
        except (ProjectMemoryError, ValueError, TypeError) as exc:
            raise ProjectCenterAutoCommandError(
                getattr(exc, "code", "canonical_prerequisite_state_unavailable"),
                str(exc),
            ) from exc
        store = self._store_for_write()
        with store._transaction() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM scope_cursors WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            if row is not None:
                status = str(row["status"] if "status" in row.keys() else "ACTIVE")
                explicit = bool(row["scope_selection_explicit"] if "scope_selection_explicit" in row.keys() else False)
                if not explicit:
                    raise ProjectCenterAutoCommandError(
                        "stale_scope_requires_explicit_start",
                        "an old cursor cannot silently authorize a new GUI scope",
                    )
                if status == "STOPPED" or row["disposition"] == "STOPPED":
                    raise ProjectCenterAutoCommandError(
                        "stopped_scope_requires_resume",
                        "the canonical STOP fence must be resumed with Wznów",
                    )
                if status == "BLOCKED" or row["disposition"] in {"BLOCKED", "HALT_BLOCKED"}:
                    raise ProjectCenterAutoCommandError(
                        "auto_blocked",
                        "canonical AUTO is durably blocked and cannot start again until its blocker is resolved",
                    )
                existing_scope = AutoScope(row["scope"])
                is_completed = status == "COMPLETED" or row["disposition"] in {"COMPLETED", "STOP_SCOPE_COMPLETE", "STOP_PROJECT_COMPLETE"}
                if is_completed:
                    if existing_scope != AutoScope.MILESTONE or requested_scope != AutoScope.MILESTONE:
                        raise ProjectCenterAutoCommandError(
                            "scope_completed",
                            "a completed non-milestone scope cannot be restarted by START_AUTO",
                        )
                    current_milestone_id = row["current_milestone_id"]
                    if current_milestone_id is None:
                        raise ProjectCenterAutoCommandError(
                            "completed_milestone_unavailable",
                            "completed MILESTONE scope has no canonical current milestone",
                        )
                    next_milestone_id = self._next_milestone_id(plan, current_milestone_id)
                    if next_milestone_id is None:
                        raise ProjectCenterAutoCommandError(
                            "no_next_milestone",
                            "the completed MILESTONE scope has no next milestone to start",
                        )
                    try:
                        _task_statuses, _prerequisite_statuses, gate_inputs = self._canonical_prerequisite_inputs(plan, conn)
                    except (ProjectMemoryError, ValueError, TypeError) as exc:
                        raise ProjectCenterAutoCommandError(
                            getattr(exc, "code", "milestone_gate_state_unavailable"),
                            str(exc),
                        ) from exc
                    if gate_inputs.get(milestone_gate_id(current_milestone_id)) != "ACCEPTED":
                        raise ProjectCenterAutoCommandError(
                            "milestone_gate_required",
                            f"milestone {current_milestone_id} is complete but its canonical gate is not accepted",
                        )

                    old_run_id = str(row["run_id"])
                    self._update_scope_history(
                        conn,
                        run_id=old_run_id,
                        scope=existing_scope,
                        milestone_id=current_milestone_id,
                        current_task_id=None,
                        lifecycle_status="completed",
                    )
                    new_run_id = f"run:project-center:{uuid.uuid4().hex}"
                    expected_revision = int(row["state_revision"])
                    new_revision = expected_revision + 1
                    changed = conn.execute(
                        """
                        UPDATE scope_cursors
                        SET run_id = ?, scope = ?, scope_epoch = ?,
                            current_milestone_id = ?, current_task_id = NULL,
                            last_accepted_task_id = NULL, last_accepted_gate = NULL,
                            plan_identity = ?, plan_version = ?, state_revision = ?,
                            disposition = 'INITIALIZED', status = 'ACTIVE',
                            stop_requested_at = NULL, stop_reason = NULL,
                            explanation_json = '{}', scope_selection_explicit = 1,
                            updated_at = ?
                        WHERE project_id = ? AND state_revision = ?
                          AND (status = 'COMPLETED' OR disposition IN ('COMPLETED', 'STOP_SCOPE_COMPLETE', 'STOP_PROJECT_COMPLETE'))
                        """,
                        (
                            new_run_id,
                            requested_scope.value,
                            int(row["scope_epoch"]) + 1,
                            next_milestone_id,
                            f"{plan.project_id}:plan:v{plan.plan_version}",
                            self._plan_version_number(plan),
                            new_revision,
                            self._now_iso(),
                            self.project_id,
                            expected_revision,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise ProjectCenterAutoCommandError("stale_cursor", "completed canonical scope changed before next start")
                    self._ensure_scope_history(
                        conn,
                        run_id=new_run_id,
                        scope=requested_scope,
                        milestone_id=next_milestone_id,
                    )
                    state = CanonicalAutoState(
                        project_id=self.project_id,
                        scope=requested_scope,
                        scope_epoch=int(row["scope_epoch"]) + 1,
                        run_id=new_run_id,
                        current_milestone_id=next_milestone_id,
                        scope_status="ACTIVE",
                        reason_code="AUTO_STARTED_NEXT_MILESTONE",
                        reason=AUTO_STATUS_REASON_TEXT["AUTO_STARTED_NEXT_MILESTONE"],
                        plan_available=True,
                        plan_version=self._plan_version_number(plan),
                        canonical_revision=new_revision,
                        **self._safe_prerequisite_projection(plan, next_milestone_id),
                    )
                    return self._receipt_from_state("START_AUTO", state, reason_code="AUTO_STARTED_NEXT_MILESTONE")

                if existing_scope != requested_scope:
                    raise ProjectCenterAutoCommandError(
                        "active_scope_cannot_change",
                        "an active canonical scope cannot be replaced by GUI selection",
                    )
                state = self.snapshot(plan_available=True, plan_version=str(plan.plan_version))
                return self._receipt_from_state(
                    "START_AUTO",
                    state,
                    reason_code="ALREADY_ACTIVE",
                    explanation="The requested canonical AUTO scope is already active.",
                    idempotent=True,
                )

            orchestrator = ScopeOrchestrator(conn, self.project_id)
            cursor = orchestrator.get_or_create_cursor(
                run_id=f"run:project-center:{uuid.uuid4().hex}",
                scope=requested_scope,
                plan_identity=f"{plan.project_id}:plan:v{plan.plan_version}",
                plan_version=int(str(plan.plan_version).split(".", 1)[0]),
                scope_selection_explicit=True,
            )
            first_milestone_id = plan.milestones[0].milestone_id
            initialized = replace(
                cursor,
                current_milestone_id=first_milestone_id,
                plan_identity=f"{plan.project_id}:plan:v{plan.plan_version}",
                plan_version=self._plan_version_number(plan),
                disposition="INITIALIZED",
            )
            if not orchestrator.update_cursor_cas(initialized, cursor.state_revision, commit=False):
                raise ProjectCenterAutoCommandError("stale_cursor", "new canonical AUTO cursor could not be initialized")
            self._ensure_scope_history(
                conn,
                run_id=cursor.run_id,
                scope=requested_scope,
                milestone_id=first_milestone_id,
            )
            state = CanonicalAutoState(
                project_id=self.project_id,
                scope=cursor.scope,
                scope_epoch=cursor.scope_epoch,
                run_id=cursor.run_id,
                current_milestone_id=first_milestone_id,
                scope_status="ACTIVE",
                continuation_status="NONE",
                reentry_status="NONE",
                reason_code="AUTO_STARTED",
                reason=f"AUTO uruchomione jawnie w scope {cursor.scope.value}.",
                plan_available=True,
                plan_version=self._plan_version_number(plan),
                canonical_revision=cursor.state_revision + 1,
                **self._safe_prerequisite_projection(plan, first_milestone_id),
            )
            return self._receipt_from_state("START_AUTO", state, reason_code="AUTO_STARTED")

    def continue_auto(self) -> AutoCommandReceipt:
        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        if not self.db_path.is_file():
            raise ProjectCenterAutoCommandError("scope_not_started", "AUTO has not been started canonically")
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        store.initialize()
        with store._transaction() as conn:
            conn.row_factory = sqlite3.Row
            orchestrator = ScopeOrchestrator(conn, self.project_id)
            row = conn.execute(
                "SELECT * FROM scope_cursors WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
            if row is None:
                raise ProjectCenterAutoCommandError("scope_not_started", "AUTO has not been started canonically")
            raw_status = str(row["status"] if "status" in row.keys() else "ACTIVE")
            disposition = str(row["disposition"] or "ACTIVE")
            if raw_status == "BLOCKED" or disposition in {"BLOCKED", "HALT_BLOCKED"}:
                raise ProjectCenterAutoCommandError(
                    "auto_blocked",
                    "canonical AUTO is durably blocked and cannot continue until its blocker is resolved",
                )
            if raw_status == "COMPLETED" or disposition in {"COMPLETED", "STOP_SCOPE_COMPLETE", "STOP_PROJECT_COMPLETE"}:
                raise ProjectCenterAutoCommandError(
                    "scope_completed_requires_new_start",
                    "completed MILESTONE scope requires an explicit START_AUTO for the next milestone",
                )
            cursor = orchestrator.get_or_create_cursor(
                run_id=row["run_id"],
                scope=AutoScope(row["scope"]),
                plan_identity=row["plan_identity"],
                plan_version=int(row["plan_version"]),
                scope_selection_explicit=bool(row["scope_selection_explicit"]),
            )
            try:
                statuses, prerequisite_statuses, gates = self._canonical_prerequisite_inputs(plan, conn)
                graph = self._plan_graph(plan)
            except (ProjectMemoryError, ValueError, TypeError) as exc:
                raise ProjectCenterAutoCommandError(
                    getattr(exc, "code", "canonical_prerequisite_state_unavailable"),
                    str(exc),
                ) from exc
            decision, explanation, updated = orchestrator.tick(
                graph,
                cursor,
                statuses,
                gates,
                prerequisite_statuses=prerequisite_statuses,
                ui_suggested_task=None,
                prompt_suggested_task=None,
            )
            if not orchestrator.update_cursor_cas(updated, cursor.state_revision, commit=False):
                raise ProjectCenterAutoCommandError("stale_cursor", "canonical AUTO continuation lost a cursor race")
            if decision.action in {ScopeAction.STOP_SCOPE_COMPLETE, ScopeAction.STOP_PROJECT_COMPLETE}:
                lifecycle_status = "completed"
            elif decision.action == ScopeAction.HALT_BLOCKED:
                lifecycle_status = "failed"
            elif decision.action == ScopeAction.HALT_WAITING_FOR_PLAN:
                lifecycle_status = "failed"
            else:
                lifecycle_status = "running"
            self._update_scope_history(
                conn,
                run_id=updated.run_id,
                scope=updated.scope,
                milestone_id=updated.current_milestone_id or cursor.current_milestone_id or plan.milestones[0].milestone_id,
                current_task_id=updated.current_task_id,
                lifecycle_status=lifecycle_status,
            )
            decision_reason_code = decision.reason_code
            decision_explanation = explanation.explanation
        state = self.snapshot(plan_available=True, plan_version=str(plan.plan_version))
        return self._receipt_from_state(
            "CONTINUE_AUTO",
            state,
            reason_code=decision_reason_code,
            explanation=decision_explanation,
        )

    def resume_auto(self) -> AutoCommandReceipt:
        if not self.db_path.is_file():
            raise ProjectCenterAutoCommandError("scope_not_started", "there is no canonical scope to resume")
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        state_before = self.snapshot(plan_available=self._plan() is not None)
        try:
            with store._transaction() as conn:
                conn.row_factory = sqlite3.Row
                execute_resume_transaction(
                    conn,
                    self.project_id,
                    expected_prior_epoch=state_before.scope_epoch,
                    actor_class="project_center",
                )
        except Exception as exc:
            code = getattr(exc, "code", "resume_rejected")
            raise ProjectCenterAutoCommandError(code, str(exc)) from exc
        state = self.snapshot(plan_available=self._plan() is not None)
        return self._receipt_from_state(
            "RESUME_AUTO",
            state,
            reason_code="AUTO_RESUMED",
            explanation="AUTO wznowione z kanonicznego Project Memory v2.",
        )

    def stop_auto(self) -> AutoCommandReceipt:
        if not self.db_path.is_file():
            raise ProjectCenterAutoCommandError("scope_not_started", "there is no canonical scope to stop")
        state_before = self.snapshot(plan_available=self._plan() is not None)
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        try:
            with store._transaction() as conn:
                conn.row_factory = sqlite3.Row
                execute_stop_transaction(
                    conn,
                    self.project_id,
                    expected_epoch=state_before.scope_epoch,
                    reason="Project Center STOP",
                    actor_class="project_center",
                )
        except Exception as exc:
            code = getattr(exc, "code", "stop_rejected")
            raise ProjectCenterAutoCommandError(code, str(exc)) from exc
        state = self.snapshot(plan_available=self._plan() is not None)
        return self._receipt_from_state(
            "STOP_AUTO",
            state,
            reason_code="STOPPED",
            explanation="STOP zapisany przez kanoniczny NX-022 STOP fence.",
        )

    def _memory_store_for_mutation(self) -> Any:
        value = self._memory_provider() if self._memory_provider is not None else ProjectMemoryStore(self.runtime_root, self.project_id)
        required = ("read_state", "set_gate_status", "set_open_question_status", "set_milestone_gate_status")
        if not all(callable(getattr(value, name, None)) for name in required):
            raise ProjectCenterAutoCommandError(
                "memory_mutation_unavailable",
                "canonical Project Memory mutation boundary is unavailable",
            )
        return value

    def _memory_mutation_receipt(
        self,
        *,
        command: str,
        identifier: str,
        expected_revision: int | None,
        target_status: str,
        read_status: Callable[[Any], str | None],
        mutate: Callable[[Any, int], str],
        reason_code: str,
        explanation: str,
    ) -> AutoCommandReceipt:
        memory = self._memory_store_for_mutation()
        before = memory.read_state()
        before_revision = int(before.revision)
        if expected_revision is not None and before_revision != expected_revision:
            raise ProjectCenterAutoCommandError(
                "stale_prerequisite",
                f"canonical prerequisite revision changed from {expected_revision} to {before_revision}",
            )
        if read_status(before) == target_status:
            state = self.snapshot(plan_available=self._plan() is not None)
            return self._receipt_from_state(
                command,
                state,
                reason_code=reason_code,
                explanation=f"{identifier} is already {target_status}; no duplicate mutation was written.",
                idempotent=True,
            )
        try:
            mutate(memory, expected_revision if expected_revision is not None else before_revision)
        except ProjectMemoryError as exc:
            raise ProjectCenterAutoCommandError(exc.code, str(exc)) from exc
        state = self.snapshot(plan_available=self._plan() is not None)
        return self._receipt_from_state(command, state, reason_code=reason_code, explanation=explanation)

    def pass_milestone_gate(self, gate_id: str, *, expected_revision: int | None = None) -> AutoCommandReceipt:
        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        self._plan_graph(plan)
        self._ensure_project_memory_plan(plan)
        try:
            identifier = str(gate_id)
            expected = {milestone_gate_id(item.milestone_id) for item in plan.milestones}
            if identifier not in expected:
                raise ProjectCenterAutoCommandError(
                    "milestone_gate_not_found",
                    f"milestone gate does not exist in the current plan: {identifier}",
                )
            return self._memory_mutation_receipt(
                command="PASS_MILESTONE_GATE",
                identifier=identifier,
                expected_revision=expected_revision,
                target_status="passed",
                read_status=lambda state: milestone_gate_statuses(plan, state).get(identifier),
                mutate=lambda memory, revision: memory.pass_milestone_gate(identifier, expected_revision=revision),
                reason_code="MILESTONE_GATE_PASSED",
                explanation=f"Milestone gate {identifier} zatwierdzono w Project Memory.",
            )
        except ProjectMemoryError as exc:
            raise ProjectCenterAutoCommandError(exc.code, str(exc)) from exc

    def approve_milestone_gate(self, gate_id: str, *, expected_revision: int | None = None) -> AutoCommandReceipt:
        return self.pass_milestone_gate(gate_id, expected_revision=expected_revision)

    def pass_gate(self, gate_id: str, *, expected_revision: int | None = None) -> AutoCommandReceipt:
        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        self._ensure_project_memory_plan(plan)
        context_gate_ids = {item["id"] for item in (plan.planning_context or {}).get("gates", [])}
        identifier = str(gate_id)
        if identifier not in context_gate_ids:
            raise ProjectCenterAutoCommandError("prerequisite_not_found", f"gate does not exist in the current plan: {identifier}")
        try:
            def read_status(state: Any) -> str | None:
                validate_prerequisite_state(plan, state)
                return planning_gate_statuses(plan, state).get(identifier)

            return self._memory_mutation_receipt(
                command="PASS_GATE",
                identifier=identifier,
                expected_revision=expected_revision,
                target_status="passed",
                read_status=read_status,
                mutate=lambda memory, revision: memory.pass_gate(identifier, expected_revision=revision),
                reason_code="GATE_PASSED",
                explanation=f"Gate {identifier} zaliczono w Project Memory.",
            )
        except ProjectMemoryError as exc:
            raise ProjectCenterAutoCommandError(exc.code, str(exc)) from exc

    def resolve_open_question(self, question_id: str, *, expected_revision: int | None = None) -> AutoCommandReceipt:
        plan = self._plan()
        if plan is None:
            raise ProjectCenterAutoCommandError("waiting_for_plan", "no canonical project plan is available")
        self._ensure_project_memory_plan(plan)
        question_ids = {item["id"] for item in (plan.planning_context or {}).get("open_questions", [])}
        identifier = str(question_id)
        if identifier not in question_ids:
            raise ProjectCenterAutoCommandError("prerequisite_not_found", f"open question does not exist in the current plan: {identifier}")
        try:
            def read_status(state: Any) -> str | None:
                validate_prerequisite_state(plan, state)
                return open_question_statuses(plan, state).get(identifier)

            return self._memory_mutation_receipt(
                command="RESOLVE_OPEN_QUESTION",
                identifier=identifier,
                expected_revision=expected_revision,
                target_status="resolved",
                read_status=read_status,
                mutate=lambda memory, revision: memory.resolve_open_question(identifier, expected_revision=revision),
                reason_code="OPEN_QUESTION_RESOLVED",
                explanation=f"Open question {identifier} rozstrzygnięto w Project Memory.",
            )
        except ProjectMemoryError as exc:
            raise ProjectCenterAutoCommandError(exc.code, str(exc)) from exc


__all__ = [
    "AUTO_SCOPE_OPTIONS",
    "AUTO_STATUS_REASON_TEXT",
    "AUTO_UI_CONTROL_CONTRACT",
    "AutoCommandReceipt",
    "AutoControlSpec",
    "CanonicalAutoState",
    "CanonicalProjectCenterAutoCommands",
    "PROJECT_CENTER_AUTO_UI_VERSION",
    "ProjectCenterAutoCommandError",
    "ProjectCenterAutoCommands",
    "ProjectCenterAutoViewModel",
]
