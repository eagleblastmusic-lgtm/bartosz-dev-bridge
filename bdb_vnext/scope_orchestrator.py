"""NX-021: Durable Scope Cursor and Orchestrator.

Determines the next legal task/run from canonical authority state:
- Immutable project plan and dependency graph
- Canonical Project Memory v2 task and gate statuses
- Persisted durable scope cursor with optimistic CAS concurrency
- Complete isolation from transient UI/prompt text
- Idempotent tick behavior (zero duplicate launches/attempts)
- Model-based plan traversal across all canonical fixtures
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from bdb_vnext.auto_scope_contract import (
    AUTO_SCOPE_SCHEMA_VERSION,
    AutoScope,
    CanonicalWorkState,
    DEFAULT_AUTO_SCOPE,
    ScopeAction,
    ScopeDecision,
    ScopeInputSnapshot,
    evaluate_scope_transition,
)
from bdb_vnext.project_memory_v2_contract import SCOPE_CURSORS_DDL

SCOPE_CURSOR_SCHEMA_VERSION = "1.0.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TERMINAL_TASK_STATUSES = frozenset({"ACCEPTED", "COMPLETED", "SKIPPED"})
_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


def _safe_graph_id(value: object) -> bool:
    return isinstance(value, str) and _GRAPH_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class ScopeCursor:
    """Durable scope cursor contract persisted under Project Memory v2 authority."""
    cursor_id: str
    project_id: str
    run_id: str
    scope: AutoScope
    scope_epoch: int = 1
    current_milestone_id: str | None = None
    current_task_id: str | None = None
    last_accepted_task_id: str | None = None
    last_accepted_gate: str | None = None
    plan_identity: str = "plan:v1"
    plan_version: int = 1
    state_revision: int = 1
    disposition: str = "INITIALIZED"
    status: str = "ACTIVE"
    stop_requested_at: str | None = None
    stop_reason: str | None = None
    explanation_json: str = "{}"
    updated_at: str = field(default_factory=_now_iso)
    scope_selection_explicit: bool = False


@dataclass(frozen=True)
class NextActionExplanation:
    """Structured explanation sufficient for audit."""
    action: str
    reason_code: str
    project_id: str
    run_id: str
    scope: str
    current_task_id: str | None
    selected_task_id: str | None
    selected_milestone_id: str | None
    dependency_evidence: Mapping[str, str]
    gate_evidence: Mapping[str, str]
    state_revision: int
    cursor_revision: int
    plan_identity: str
    plan_version: int
    canonical_work_state: str
    explanation: str


@dataclass(frozen=True)
class PlanTaskNode:
    task_id: str
    milestone_id: str
    dependencies: tuple[str, ...] = ()
    is_gate: bool = False
    requires_manual_approval: bool = False
    requires_policy_approval: bool = False


@dataclass(frozen=True)
class PlanPrerequisiteNode:
    """A non-task dependency declared by the immutable project plan."""

    prerequisite_id: str
    kind: str


@dataclass(frozen=True)
class PlanMilestoneNode:
    milestone_id: str
    gate_id: str
    dependencies: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalPlanGraph:
    plan_identity: str
    plan_version: int
    milestones: tuple[PlanMilestoneNode, ...]
    tasks: tuple[PlanTaskNode, ...]
    plan_digest: str | None = None
    prerequisites: tuple[PlanPrerequisiteNode, ...] = ()

    def canonical_document(self) -> dict[str, Any]:
        """Return the bounded, deterministic representation of this plan graph."""
        document: dict[str, Any] = {
            "plan_identity": self.plan_identity,
            "plan_version": self.plan_version,
            "milestones": [
                {
                    "milestone_id": milestone.milestone_id,
                    "gate_id": milestone.gate_id,
                    "dependencies": list(milestone.dependencies),
                    "task_ids": list(milestone.task_ids),
                }
                for milestone in self.milestones
            ],
            "tasks": [
                {
                    "task_id": task.task_id,
                    "milestone_id": task.milestone_id,
                    "dependencies": list(task.dependencies),
                    "is_gate": task.is_gate,
                    "requires_manual_approval": task.requires_manual_approval,
                    "requires_policy_approval": task.requires_policy_approval,
                }
                for task in self.tasks
            ],
        }
        if self.prerequisites:
            document["prerequisites"] = [
                {
                    "prerequisite_id": prerequisite.prerequisite_id,
                    "kind": prerequisite.kind,
                }
                for prerequisite in self.prerequisites
            ]
        return document

    def computed_plan_digest(self) -> str:
        """Return a content digest for the exact graph and identity."""
        payload = json.dumps(
            self.canonical_document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def canonical_plan_digest(self) -> str:
        """Return the declared digest only when it matches the graph bytes."""
        computed = self.computed_plan_digest()
        if self.plan_digest is not None and self.plan_digest != computed:
            raise ValueError("plan_digest does not match canonical plan graph")
        return self.plan_digest or computed

    def get_task(self, task_id: str) -> PlanTaskNode | None:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def get_milestone(self, milestone_id: str) -> PlanMilestoneNode | None:
        for m in self.milestones:
            if m.milestone_id == milestone_id:
                return m
        return None

    def get_prerequisite(self, prerequisite_id: str) -> PlanPrerequisiteNode | None:
        for prerequisite in self.prerequisites:
            if prerequisite.prerequisite_id == prerequisite_id:
                return prerequisite
        return None

    def dependency_kind(self, dependency_id: str) -> str | None:
        if self.get_task(dependency_id) is not None:
            return "task"
        prerequisite = self.get_prerequisite(dependency_id)
        return prerequisite.kind if prerequisite is not None else None

    def validate_graph(self) -> tuple[bool, str]:
        """Detects circular dependencies or broken references (fails closed)."""
        if any(
            not _safe_graph_id(identifier)
            for identifier in (
                *(task.task_id for task in self.tasks),
                *(task.milestone_id for task in self.tasks),
                *(milestone.milestone_id for milestone in self.milestones),
                *(milestone.gate_id for milestone in self.milestones),
                *(prerequisite.prerequisite_id for prerequisite in self.prerequisites),
                *(task_id for milestone in self.milestones for task_id in milestone.task_ids),
                *(dependency for task in self.tasks for dependency in task.dependencies),
                *(dependency for milestone in self.milestones for dependency in milestone.dependencies),
            )
        ):
            return False, "Plan graph contains an unsafe or empty identifier"

        task_ids = {t.task_id for t in self.tasks}
        ms_ids = {m.milestone_id for m in self.milestones}

        if len(task_ids) != len(self.tasks):
            return False, "Plan graph contains duplicate task IDs"
        if len(ms_ids) != len(self.milestones):
            return False, "Plan graph contains duplicate milestone IDs"
        gate_ids = [milestone.gate_id for milestone in self.milestones]
        if any(not gate_id for gate_id in gate_ids):
            return False, "Plan graph contains an empty milestone gate ID"
        if len(set(gate_ids)) != len(gate_ids):
            return False, "Plan graph contains duplicate milestone gate IDs"
        if set(task_ids) & set(gate_ids):
            return False, "Plan graph contains a task/milestone-gate namespace collision"
        if set(ms_ids) & set(gate_ids):
            return False, "Plan graph contains a milestone/milestone-gate namespace collision"

        for milestone in self.milestones:
            if len(set(milestone.task_ids)) != len(milestone.task_ids):
                return False, f"Milestone {milestone.milestone_id} lists a task more than once"
            if len(set(milestone.dependencies)) != len(milestone.dependencies):
                return False, f"Milestone {milestone.milestone_id} lists a dependency more than once"
        for task in self.tasks:
            if len(set(task.dependencies)) != len(task.dependencies):
                return False, f"Task {task.task_id} lists a dependency more than once"

        task_memberships: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        for milestone in self.milestones:
            for task_id in milestone.task_ids:
                if task_id not in task_ids:
                    return False, f"Milestone {milestone.milestone_id} references non-existent task {task_id}"
                task_memberships[task_id].append(milestone.milestone_id)
        for task in self.tasks:
            memberships = task_memberships[task.task_id]
            if memberships != [task.milestone_id]:
                return False, f"Task {task.task_id} is not listed exactly once in its milestone"

        milestone_graph: dict[str, set[str]] = {}
        for milestone in self.milestones:
            for dependency in milestone.dependencies:
                if dependency not in ms_ids:
                    return False, f"Milestone {milestone.milestone_id} references non-existent dependency {dependency}"
            milestone_graph[milestone.milestone_id] = set(milestone.dependencies)

        milestone_visited: dict[str, int] = {}

        def has_milestone_cycle(milestone_id: str) -> bool:
            milestone_visited[milestone_id] = 0
            for dependency in milestone_graph[milestone_id]:
                if dependency in milestone_visited:
                    if milestone_visited[dependency] == 0:
                        return True
                elif has_milestone_cycle(dependency):
                    return True
            milestone_visited[milestone_id] = 1
            return False

        for milestone_id in ms_ids:
            if milestone_id not in milestone_visited and has_milestone_cycle(milestone_id):
                return False, f"Circular milestone dependency detected involving {milestone_id}"

        prerequisite_kinds: dict[str, str] = {}
        for prerequisite in self.prerequisites:
            if prerequisite.kind not in {"gate", "open_question"}:
                return False, f"Prerequisite {prerequisite.prerequisite_id} has an unsupported namespace"
            if not prerequisite.prerequisite_id:
                return False, "Plan graph contains an empty prerequisite ID"
            if prerequisite.prerequisite_id in task_ids:
                return False, f"Dependency target {prerequisite.prerequisite_id} has multiple namespaces"
            if prerequisite.prerequisite_id in gate_ids:
                return False, f"Dependency target {prerequisite.prerequisite_id} collides with a milestone gate"
            if prerequisite.prerequisite_id in prerequisite_kinds:
                return False, f"Prerequisite {prerequisite.prerequisite_id} is declared more than once"
            prerequisite_kinds[prerequisite.prerequisite_id] = prerequisite.kind

        # Check references
        for t in self.tasks:
            if t.milestone_id not in ms_ids:
                return False, f"Task {t.task_id} references non-existent milestone {t.milestone_id}"
            for dep in t.dependencies:
                if dep in task_ids and dep in prerequisite_kinds:
                    return False, f"Dependency target {dep} has multiple namespaces"
                if dep not in task_ids and dep not in prerequisite_kinds:
                    return False, f"Task {t.task_id} references non-existent dependency {dep}"

        # Cycle detection
        visited: dict[str, int] = {}  # 0: visiting, 1: visited
        def has_cycle(tid: str) -> bool:
            visited[tid] = 0
            task = self.get_task(tid)
            if task:
                for dep in task.dependencies:
                    if dep in visited:
                        if visited[dep] == 0:
                            return True
                    else:
                        if has_cycle(dep):
                            return True
            visited[tid] = 1
            return False

        for tid in task_ids:
            if tid not in visited:
                if has_cycle(tid):
                    return False, f"Circular dependency detected involving task {tid}"

        return True, "valid"


def _blocked_cursor(
    cursor: ScopeCursor,
    *,
    plan: CanonicalPlanGraph,
    explanation: NextActionExplanation,
) -> ScopeCursor:
    return replace(
        cursor,
        plan_identity=plan.plan_identity,
        plan_version=plan.plan_version,
        disposition=ScopeAction.HALT_BLOCKED.value,
        status="BLOCKED",
        explanation_json=json.dumps(asdict(explanation)),
        updated_at=_now_iso(),
    )


def dependency_state(
    plan: CanonicalPlanGraph,
    dependency_id: str,
    task_statuses: Mapping[str, str],
    prerequisite_statuses: Mapping[str, str] | None = None,
) -> tuple[str, str, bool]:
    """Return ``(kind, canonical_status, satisfied)`` for one dependency.

    Task statuses are the orchestrator's uppercase execution statuses.  Gate
    and open-question statuses are the lowercase values owned by Project
    Memory.  Missing external status entries intentionally resolve to their
    canonical unsatisfied defaults.
    """

    kind = plan.dependency_kind(dependency_id)
    if kind == "task":
        status = str(task_statuses.get(dependency_id, "NOT_STARTED")).upper()
        return kind, status, status in _TERMINAL_TASK_STATUSES

    statuses = prerequisite_statuses or {}
    if kind == "gate":
        status = str(statuses.get(dependency_id, "pending")).lower()
        return kind, status, status == "passed"
    if kind == "open_question":
        status = str(statuses.get(dependency_id, "open")).lower()
        return kind, status, status == "resolved"
    return "unknown", "UNKNOWN", False


def canonical_milestone_gate_statuses(
    plan: CanonicalPlanGraph,
    statuses: Mapping[str, str],
) -> dict[str, str]:
    """Normalize milestone gate evidence to each node's canonical gate ID.

    Older callers supplied milestone IDs as keys.  They are accepted only as
    a read-compatibility input and are immediately converted to the emitted
    ``PlanMilestoneNode.gate_id`` namespace.  Conflicting aliases fail closed.
    """

    normalized: dict[str, str] = {}
    for milestone in plan.milestones:
        canonical = statuses.get(milestone.gate_id)
        legacy = statuses.get(milestone.milestone_id) if milestone.milestone_id != milestone.gate_id else None
        canonical_value = str(canonical).upper() if canonical is not None else None
        legacy_value = str(legacy).upper() if legacy is not None else None
        if canonical_value is not None and legacy_value is not None and canonical_value != legacy_value:
            raise ValueError(f"milestone {milestone.milestone_id} has conflicting gate status identities")
        normalized[milestone.gate_id] = canonical_value or legacy_value or "NOT_REACHED"
    return normalized


def milestone_gate_key(plan: CanonicalPlanGraph, milestone_reference: str) -> str:
    milestone = plan.get_milestone(milestone_reference)
    return milestone.gate_id if milestone is not None else milestone_reference


class ScopeOrchestrator:
    """Canonical Scope Orchestrator managing durable cursor and state evaluation."""

    CURSOR_STORAGE_DATABASE: str = "memory.db"
    CURSOR_SCHEMA_OWNER: str = "ProjectMemoryStoreV2"
    CURSOR_MIGRATION_OWNER: str = "ProjectMemoryStoreV2"
    CURSOR_TRANSACTION_AUTHORITY: str = "ProjectMemoryStoreV2._transaction"
    CURSOR_UNDER_PROJECT_MEMORY_V2_AUTHORITY: bool = True
    SECOND_CURSOR_AUTHORITY_CREATED: bool = False
    CURSOR_SCHEMA_VERSIONED: bool = True

    # NX-022 Authority Declarations
    STOP_FENCE_UNDER_PROJECT_MEMORY_V2_AUTHORITY: bool = True
    SECOND_STOP_AUTHORITY_CREATED: bool = False
    STOP_FENCE_SCHEMA_VERSION: str = "1.0.0"

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.conn = conn
        self.project_id = project_id
        self._ensure_canonical_schema()

    def _ensure_canonical_schema(self) -> None:
        from bdb_vnext.project_memory_v2_contract import STOP_FENCES_DDL
        self.conn.executescript(SCOPE_CURSORS_DDL)
        self.conn.executescript(STOP_FENCES_DDL)
        # Migrate existing table if status / stop columns are missing
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(scope_cursors)").fetchall()}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE scope_cursors ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'")
        if "stop_requested_at" not in columns:
            self.conn.execute("ALTER TABLE scope_cursors ADD COLUMN stop_requested_at TEXT")
        if "stop_reason" not in columns:
            self.conn.execute("ALTER TABLE scope_cursors ADD COLUMN stop_reason TEXT")
        if "scope_selection_explicit" not in columns:
            self.conn.execute(
                "ALTER TABLE scope_cursors ADD COLUMN scope_selection_explicit INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.commit()

    def get_or_create_cursor(
        self,
        run_id: str,
        scope: AutoScope = DEFAULT_AUTO_SCOPE,
        plan_identity: str = "plan:v1",
        plan_version: int = 1,
        scope_selection_explicit: bool = False,
    ) -> ScopeCursor:
        row = self.conn.execute(
            "SELECT * FROM scope_cursors WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()

        if row:
            keys = row.keys()
            status = row["status"] if "status" in keys else "ACTIVE"
            stop_requested_at = row["stop_requested_at"] if "stop_requested_at" in keys else None
            stop_reason = row["stop_reason"] if "stop_reason" in keys else None
            explicit_selection = bool(
                row["scope_selection_explicit"]
                if "scope_selection_explicit" in keys
                else False
            )
            return ScopeCursor(
                cursor_id=row["cursor_id"],
                project_id=row["project_id"],
                run_id=row["run_id"],
                scope=AutoScope(row["scope"]),
                scope_epoch=row["scope_epoch"],
                current_milestone_id=row["current_milestone_id"],
                current_task_id=row["current_task_id"],
                last_accepted_task_id=row["last_accepted_task_id"],
                last_accepted_gate=row["last_accepted_gate"],
                plan_identity=row["plan_identity"],
                plan_version=row["plan_version"],
                state_revision=row["state_revision"],
                disposition=row["disposition"],
                status=status,
                stop_requested_at=stop_requested_at,
                stop_reason=stop_reason,
                explanation_json=row["explanation_json"],
                updated_at=row["updated_at"],
                scope_selection_explicit=explicit_selection,
            )

        now_iso = _now_iso()
        cursor_id = f"cur-{self.project_id}"
        self.conn.execute(
            """
            INSERT INTO scope_cursors (
                cursor_id, project_id, run_id, scope, scope_epoch,
                current_milestone_id, current_task_id, last_accepted_task_id,
                last_accepted_gate, plan_identity, plan_version, state_revision,
                disposition, status, stop_requested_at, stop_reason, explanation_json,
                scope_selection_explicit, updated_at
            ) VALUES (?, ?, ?, ?, 1, NULL, NULL, NULL, NULL, ?, ?, 1, 'INITIALIZED', 'ACTIVE', NULL, NULL, '{}', ?, ?)
            """,
            (
                cursor_id,
                self.project_id,
                run_id,
                scope.value,
                plan_identity,
                plan_version,
                int(scope_selection_explicit),
                now_iso,
            ),
        )
        self.conn.commit()

        return ScopeCursor(
            cursor_id=cursor_id,
            project_id=self.project_id,
            run_id=run_id,
            scope=scope,
            scope_epoch=1,
            plan_identity=plan_identity,
            plan_version=plan_version,
            state_revision=1,
            disposition="INITIALIZED",
            status="ACTIVE",
            stop_requested_at=None,
            stop_reason=None,
            updated_at=now_iso,
            scope_selection_explicit=scope_selection_explicit,
        )

    def update_cursor_cas(
        self,
        cursor: ScopeCursor,
        expected_revision: int,
        *,
        commit: bool = True,
    ) -> bool:
        """Optimistic CAS update of durable cursor."""
        new_revision = expected_revision + 1
        now_iso = _now_iso()

        cur = self.conn.execute(
            """
            UPDATE scope_cursors
            SET run_id = ?,
                scope = ?,
                scope_epoch = ?,
                current_milestone_id = ?,
                current_task_id = ?,
                last_accepted_task_id = ?,
                last_accepted_gate = ?,
                plan_identity = ?,
                plan_version = ?,
                state_revision = ?,
                disposition = ?,
                status = ?,
                stop_requested_at = ?,
                stop_reason = ?,
                explanation_json = ?,
                scope_selection_explicit = ?,
                updated_at = ?
            WHERE project_id = ? AND state_revision = ?
            """,
            (
                cursor.run_id,
                cursor.scope.value,
                cursor.scope_epoch,
                cursor.current_milestone_id,
                cursor.current_task_id,
                cursor.last_accepted_task_id,
                cursor.last_accepted_gate,
                cursor.plan_identity,
                cursor.plan_version,
                new_revision,
                cursor.disposition,
                cursor.status,
                cursor.stop_requested_at,
                cursor.stop_reason,
                cursor.explanation_json,
                int(cursor.scope_selection_explicit),
                now_iso,
                self.project_id,
                expected_revision,
            ),
        )
        if cur.rowcount == 0:
            # Concurrency conflict / stale cursor
            return False

        if commit:
            self.conn.commit()
        return True

    def resolve_next_runnable_task(
        self,
        plan: CanonicalPlanGraph,
        milestone_id: str,
        task_statuses: Mapping[str, str],
        prerequisite_statuses: Mapping[str, str] | None = None,
    ) -> tuple[str | None, bool, list[str]]:
        """Resolves the next task in milestone, whether deps are met, and pending dep IDs."""
        ms = plan.get_milestone(milestone_id)
        if not ms:
            return None, False, []

        for tid in ms.task_ids:
            st = task_statuses.get(tid, "NOT_STARTED")
            # Invariant: An accepted task is NEVER runnable again!
            if str(st).upper() in _TERMINAL_TASK_STATUSES:
                continue

            # Found candidate unaccepted task
            task_node = plan.get_task(tid)
            if not task_node:
                continue

            pending_deps = [
                dep
                for dep in task_node.dependencies
                if not dependency_state(plan, dep, task_statuses, prerequisite_statuses)[2]
            ]
            deps_ok = len(pending_deps) == 0
            return tid, deps_ok, pending_deps

        return None, True, []

    def tick(
        self,
        plan: CanonicalPlanGraph,
        cursor: ScopeCursor,
        task_statuses: Mapping[str, str],
        milestone_gate_statuses: Mapping[str, str],
        *,
        prerequisite_statuses: Mapping[str, str] | None = None,
        manual_approvals: Mapping[str, bool] | None = None,
        policy_approvals: Mapping[str, bool] | None = None,
        stop_requested: bool = False,
        ci_waiting_tasks: Sequence[str] = (),
        transient_backoff_tasks: Sequence[str] = (),
        approved_plan_exhausted: bool = False,
        ui_suggested_task: str | None = None,  # Explicitly rejected as canonical authority
        prompt_suggested_task: str | None = None,  # Explicitly rejected as canonical authority
    ) -> tuple[ScopeDecision, NextActionExplanation, ScopeCursor]:
        """Core deterministic orchestrator tick."""
        # 1. Validate plan graph first (fail closed on ambiguity / cycles)
        is_valid, err_msg = plan.validate_graph()
        if not is_valid:
            dec = ScopeDecision(
                action=ScopeAction.HALT_BLOCKED,
                canonical_work_state=CanonicalWorkState.BLOCKED,
                reason_code="AMBIGUOUS_PLAN_GRAPH",
                explanation=f"Plan graph validation failed: {err_msg}",
                is_terminal=True,
            )
            expl = NextActionExplanation(
                action=dec.action.value,
                reason_code=dec.reason_code,
                project_id=self.project_id,
                run_id=cursor.run_id,
                scope=cursor.scope.value,
                current_task_id=cursor.current_task_id,
                selected_task_id=None,
                selected_milestone_id=None,
                dependency_evidence={},
                gate_evidence={},
                state_revision=cursor.state_revision,
                cursor_revision=cursor.state_revision,
                plan_identity=plan.plan_identity,
                plan_version=plan.plan_version,
                canonical_work_state=dec.canonical_work_state.value,
                explanation=dec.explanation,
            )
            updated_cursor = _blocked_cursor(cursor, plan=plan, explanation=expl)
            return dec, expl, updated_cursor

        try:
            canonical_gate_statuses = canonical_milestone_gate_statuses(plan, milestone_gate_statuses)
        except ValueError as exc:
            dec = ScopeDecision(
                action=ScopeAction.HALT_BLOCKED,
                canonical_work_state=CanonicalWorkState.BLOCKED,
                reason_code="AMBIGUOUS_MILESTONE_GATE_STATUS",
                explanation=f"Milestone gate status validation failed: {exc}",
                is_terminal=True,
            )
            expl = NextActionExplanation(
                action=dec.action.value,
                reason_code=dec.reason_code,
                project_id=self.project_id,
                run_id=cursor.run_id,
                scope=cursor.scope.value,
                current_task_id=cursor.current_task_id,
                selected_task_id=None,
                selected_milestone_id=None,
                dependency_evidence={},
                gate_evidence={},
                state_revision=cursor.state_revision,
                cursor_revision=cursor.state_revision,
                plan_identity=plan.plan_identity,
                plan_version=plan.plan_version,
                canonical_work_state=dec.canonical_work_state.value,
                explanation=dec.explanation,
            )
            updated_cursor = _blocked_cursor(cursor, plan=plan, explanation=expl)
            return dec, expl, updated_cursor

        # 2. Determine current milestone
        cur_ms_id = cursor.current_milestone_id or (plan.milestones[0].milestone_id if plan.milestones else None)
        if not cur_ms_id or not plan.get_milestone(cur_ms_id):
            dec = ScopeDecision(
                action=ScopeAction.HALT_BLOCKED,
                canonical_work_state=CanonicalWorkState.BLOCKED,
                reason_code="INVALID_MILESTONE_CURSOR",
                explanation=f"Cursor milestone {cur_ms_id} not found in plan.",
                is_terminal=True,
            )
            expl = NextActionExplanation(
                action=dec.action.value,
                reason_code=dec.reason_code,
                project_id=self.project_id,
                run_id=cursor.run_id,
                scope=cursor.scope.value,
                current_task_id=cursor.current_task_id,
                selected_task_id=None,
                selected_milestone_id=None,
                dependency_evidence={},
                gate_evidence={},
                state_revision=cursor.state_revision,
                cursor_revision=cursor.state_revision,
                plan_identity=plan.plan_identity,
                plan_version=plan.plan_version,
                canonical_work_state=dec.canonical_work_state.value,
                explanation=dec.explanation,
            )
            return dec, expl, _blocked_cursor(cursor, plan=plan, explanation=expl)

        cur_ms = plan.get_milestone(cur_ms_id)
        assert cur_ms is not None

        # Check milestone ordering / dependencies (fail closed on wrong milestone cursor)
        ms_index = [m.milestone_id for m in plan.milestones].index(cur_ms_id)
        for prev_ms in plan.milestones[:ms_index]:
            if canonical_gate_statuses.get(prev_ms.gate_id) != "ACCEPTED":
                dec = ScopeDecision(
                    action=ScopeAction.HALT_BLOCKED,
                    canonical_work_state=CanonicalWorkState.BLOCKED,
                    reason_code="WRONG_MILESTONE_CURSOR",
                    explanation=f"Cannot operate in milestone {cur_ms_id} because previous milestone {prev_ms.milestone_id} gate is not accepted.",
                    is_terminal=True,
                )
                expl = NextActionExplanation(
                    action=dec.action.value,
                    reason_code=dec.reason_code,
                    project_id=self.project_id,
                    run_id=cursor.run_id,
                    scope=cursor.scope.value,
                    current_task_id=cursor.current_task_id,
                    selected_task_id=None,
                    selected_milestone_id=None,
                    dependency_evidence={},
                    gate_evidence=canonical_gate_statuses,
                    state_revision=cursor.state_revision,
                    cursor_revision=cursor.state_revision,
                    plan_identity=plan.plan_identity,
                    plan_version=plan.plan_version,
                    canonical_work_state=dec.canonical_work_state.value,
                    explanation=dec.explanation,
                )
                return dec, expl, _blocked_cursor(cursor, plan=plan, explanation=expl)

        # 3. Check milestone task completion and gates
        all_ms_tasks_accepted = (
            len(cur_ms.task_ids) > 0
            and all(
                str(task_statuses.get(tid, "NOT_STARTED")).upper() in _TERMINAL_TASK_STATUSES
                for tid in cur_ms.task_ids
            )
        )
        cur_gate_status = canonical_gate_statuses.get(cur_ms.gate_id, "NOT_REACHED")

        # Next task in milestone
        next_tid, deps_satisfied, pending_deps = self.resolve_next_runnable_task(
            plan,
            cur_ms_id,
            task_statuses,
            prerequisite_statuses,
        )

        # Check next milestone if current is done
        next_ms_id = plan.milestones[ms_index + 1].milestone_id if (ms_index + 1 < len(plan.milestones)) else None
        next_ms_deps_ok = True
        first_task_in_next_ms = None
        if next_ms_id:
            next_ms = plan.get_milestone(next_ms_id)
            if next_ms:
                next_ms_deps_ok = all(
                    canonical_gate_statuses.get(milestone_gate_key(plan, dep)) == "ACCEPTED"
                    for dep in next_ms.dependencies
                )
                first_task_in_next_ms, _, _ = self.resolve_next_runnable_task(
                    plan,
                    next_ms_id,
                    task_statuses,
                    prerequisite_statuses,
                )

        all_proj_done = (
            ms_index == len(plan.milestones) - 1
            and all_ms_tasks_accepted
            and cur_gate_status == "ACCEPTED"
        )

        # Approvals
        manual_app = (manual_approvals or {})
        policy_app = (policy_approvals or {})
        active_task_node = plan.get_task(next_tid) if next_tid else None
        man_req = bool(active_task_node and active_task_node.requires_manual_approval)
        man_ok = manual_app.get(next_tid or "", False) if man_req else True
        pol_req = bool(active_task_node and active_task_node.requires_policy_approval)
        pol_ok = policy_app.get(next_tid or "", False) if pol_req else True

        # Build snapshot
        raw_cur_tid_status = task_statuses.get(cursor.current_task_id or "") if cursor.current_task_id else None
        cur_tid_status = str(raw_cur_tid_status).upper() if raw_cur_tid_status is not None else None
        accepted_count = 1 if (cur_tid_status in _TERMINAL_TASK_STATUSES) else 0
        effective_stop = (
            stop_requested
            or cursor.status == "STOPPED"
            or cursor.disposition == "STOPPED"
        )

        snapshot = ScopeInputSnapshot(
            current_scope=cursor.scope,
            current_task_id=cursor.current_task_id,
            current_task_status=cur_tid_status,
            accepted_tasks_in_current_scope=accepted_count,
            current_milestone_id=cur_ms_id,
            all_milestone_tasks_accepted=all_ms_tasks_accepted,
            current_milestone_gate_status=cur_gate_status,
            next_task_in_milestone_id=next_tid,
            next_task_dependencies_satisfied=deps_satisfied,
            next_milestone_id=next_ms_id,
            first_task_in_next_milestone_id=first_task_in_next_ms,
            next_milestone_dependencies_satisfied=next_ms_deps_ok,
            all_project_milestones_completed=all_proj_done,
            manual_gate_required=man_req,
            manual_gate_approved=man_ok,
            policy_gate_required=pol_req,
            policy_gate_approved=pol_ok,
            stop_requested=effective_stop,
            ci_waiting=bool(cursor.current_task_id and cursor.current_task_id in ci_waiting_tasks),
            transient_backoff=bool(cursor.current_task_id and cursor.current_task_id in transient_backoff_tasks),
            approved_plan_exhausted=approved_plan_exhausted,
        )

        # 4. Evaluate via canonical contract
        decision = evaluate_scope_transition(snapshot)

        # Build evidence
        dep_evidence = {
            d: dependency_state(plan, d, task_statuses, prerequisite_statuses)[1]
            for d in pending_deps
        }
        gate_evidence = {cur_ms.gate_id: cur_gate_status}

        explanation = NextActionExplanation(
            action=decision.action.value,
            reason_code=decision.reason_code,
            project_id=self.project_id,
            run_id=cursor.run_id,
            scope=cursor.scope.value,
            current_task_id=cursor.current_task_id,
            selected_task_id=decision.selected_task_id,
            selected_milestone_id=decision.selected_milestone_id,
            dependency_evidence=dep_evidence,
            gate_evidence=gate_evidence,
            state_revision=cursor.state_revision,
            cursor_revision=cursor.state_revision,
            plan_identity=plan.plan_identity,
            plan_version=plan.plan_version,
            canonical_work_state=decision.canonical_work_state.value,
            explanation=decision.explanation,
        )

        # 5. Advance cursor state
        new_ms = decision.selected_milestone_id or cur_ms_id
        new_task = decision.selected_task_id or cursor.current_task_id
        if decision.action in {
            ScopeAction.STOP_SCOPE_COMPLETE,
            ScopeAction.STOP_PROJECT_COMPLETE,
            ScopeAction.HALT_WAITING_FOR_PLAN,
        }:
            new_task = None
        last_accepted_t = cursor.current_task_id if cur_tid_status in _TERMINAL_TASK_STATUSES else cursor.last_accepted_task_id
        last_accepted_g = cur_ms.gate_id if cur_gate_status == "ACCEPTED" else cursor.last_accepted_gate

        cursor_status = cursor.status
        if decision.action == ScopeAction.HALT_BLOCKED:
            cursor_status = "BLOCKED"
        elif decision.action in {ScopeAction.STOP_SCOPE_COMPLETE, ScopeAction.STOP_PROJECT_COMPLETE}:
            cursor_status = "COMPLETED"
        elif decision.action == ScopeAction.HALT_WAITING_FOR_PLAN:
            cursor_status = "WAITING_FOR_PLAN"

        updated_cursor = ScopeCursor(
            cursor_id=cursor.cursor_id,
            project_id=self.project_id,
            run_id=cursor.run_id,
            scope=cursor.scope,
            scope_epoch=cursor.scope_epoch,
            current_milestone_id=new_ms,
            current_task_id=new_task,
            last_accepted_task_id=last_accepted_t,
            last_accepted_gate=last_accepted_g,
            plan_identity=plan.plan_identity,
            plan_version=plan.plan_version,
            state_revision=cursor.state_revision,  # update_cursor_cas increments this
            disposition=decision.action.value,
            status=cursor_status,
            stop_requested_at=cursor.stop_requested_at,
            stop_reason=cursor.stop_reason,
            explanation_json=json.dumps(asdict(explanation)),
            updated_at=_now_iso(),
            scope_selection_explicit=cursor.scope_selection_explicit,
        )

        return decision, explanation, updated_cursor

    def request_stop(
        self,
        *,
        expected_epoch: int | None = None,
        reason: str = "External STOP requested",
        actor_class: str = "operator",
    ) -> tuple[Any, bool, int, int]:
        """Atomically executes a STOP transaction under Project Memory v2 authority."""
        from bdb_vnext.stop_fence import execute_stop_transaction
        return execute_stop_transaction(
            self.conn,
            self.project_id,
            expected_epoch=expected_epoch,
            reason=reason,
            actor_class=actor_class,
        )

    def resume_scope(
        self,
        *,
        expected_prior_epoch: int | None = None,
        new_run_id: str | None = None,
        actor_class: str = "operator",
    ) -> Any:
        """Atomically resumes execution into a new monotonic epoch (N -> N+1)."""
        from bdb_vnext.stop_fence import execute_resume_transaction
        return execute_resume_transaction(
            self.conn,
            self.project_id,
            expected_prior_epoch=expected_prior_epoch,
            new_run_id=new_run_id,
            actor_class=actor_class,
        )
