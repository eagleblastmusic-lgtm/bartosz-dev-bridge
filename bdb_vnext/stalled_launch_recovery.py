"""Explicit, same-binding recovery for stalled project launch projections.

This module intentionally does not participate in normal outbox reconciliation.
A PUBLISHED launch may already have been consumed by Browser/Native while the
canonical outbox still says PUBLISHED, so automatic replay would risk duplicate
execution. Recovery is therefore an explicit operator action guarded by the
canonical watchdog and the active binding identity.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from typing import Any

from .project_execution import ProjectExecutionBinding
from .project_workflow import ProjectWorkflow, ProjectWorkflowError


class StalledLaunchRecoveryError(RuntimeError):
    """Fail-closed error raised when same-binding replay is not provably safe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StalledLaunchRecoveryResult:
    status: str
    project_id: str
    task_id: str
    execution_binding_id: str
    launch_id: str
    generation: int
    outbox_status: str
    queue_launch_id: str | None
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "execution_binding_id": self.execution_binding_id,
            "launch_id": self.launch_id,
            "generation": self.generation,
            "outbox_status": self.outbox_status,
            "queue_launch_id": self.queue_launch_id,
            "applied": self.applied,
        }


def _fail(code: str, message: str) -> None:
    raise StalledLaunchRecoveryError(code, message)


def _validate_recovery_target(
    workflow: ProjectWorkflow,
    project_id: str,
    execution_binding_id: str,
) -> tuple[ProjectExecutionBinding, Any]:
    watchdog = workflow.execution.watchdog(project_id)
    if (
        watchdog.get("state") != "STALLED"
        or watchdog.get("resume_available") is not True
        or watchdog.get("execution_binding_id") != execution_binding_id
    ):
        _fail(
            "execution_not_stalled",
            "same-binding launch recovery requires the requested binding to be the canonical STALLED execution",
        )

    binding = workflow.execution.resume_binding(project_id, execution_binding_id)
    if binding.conversation_id:
        _fail(
            "launch_has_conversation",
            "a stalled binding already associated with a conversation must not be replayed automatically",
        )

    handoff = workflow.execution.launch_handoff(project_id, binding.execution_binding_id)
    if handoff is not None and handoff.get("status") == "SENT":
        _fail(
            "launch_handoff_already_sent",
            "a launch with a completed Browser handoff must not be replayed",
        )

    outbox = workflow.execution.launch_outbox_record(project_id, binding.launch_id)
    if outbox is None:
        _fail("launch_outbox_not_found", "active stalled binding has no canonical launch outbox record")
    if (
        outbox.project_id != project_id
        or outbox.execution_binding_id != binding.execution_binding_id
        or outbox.task_id != binding.task_id
        or outbox.launch_id != binding.launch_id
    ):
        _fail("launch_outbox_conflict", "launch outbox identity differs from the active stalled binding")
    if outbox.status == "ACKNOWLEDGED":
        _fail("launch_already_acknowledged", "acknowledged launches must not be replayed")
    if outbox.status not in {"PENDING", "PUBLISHED"}:
        _fail("launch_outbox_status_invalid", f"unsupported recovery outbox status: {outbox.status}")

    queued = workflow.queue.peek()
    if queued is not None and queued.launch_id != binding.launch_id:
        _fail("queue_pending", "project launch queue already contains another launch")

    return binding, outbox


def _reactivate_same_binding(
    workflow: ProjectWorkflow,
    project_id: str,
    binding: ProjectExecutionBinding,
) -> None:
    """Reactivate only the task owned by the already-active canonical binding."""

    memory = workflow.memory(project_id)
    plan = memory.current_plan()
    if plan is None:
        _fail("project_plan_required", "project execution requires an imported plan")

    def transition(state):
        execution = dict(state.execution or {})
        bindings = list(execution.get("bindings", []))
        raw = next(
            (item for item in bindings if item.get("execution_binding_id") == binding.execution_binding_id),
            None,
        )
        if raw is None:
            _fail("execution_binding_not_found", "execution binding does not exist in canonical memory")
        if (
            raw.get("project_id") != project_id
            or str(raw.get("plan_version")) != str(plan.plan_version)
            or raw.get("status", "ACTIVE") != "ACTIVE"
            or raw.get("superseded") is True
            or raw.get("task_id") != binding.task_id
            or raw.get("launch_id") != binding.launch_id
        ):
            _fail("execution_binding_stale", "execution binding is no longer the active canonical identity")

        current_binding_id = execution.get("current_binding_id")
        if current_binding_id not in (None, binding.execution_binding_id):
            _fail("execution_binding_stale", "another binding is the current canonical binding")

        task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
        if task is None:
            _fail("task_not_found", "execution task does not exist in the current plan")

        statuses = dict(execution.get("task_statuses", {}))
        current_status = statuses.get(binding.task_id, task.status)
        if current_status in {"completed", "skipped"}:
            _fail("task_already_complete", "completed task cannot be reactivated")

        statuses[binding.task_id] = "active"
        execution["task_statuses"] = statuses
        execution["current_task_id"] = binding.task_id
        execution["current_binding_id"] = binding.execution_binding_id

        updated = replace(state, execution=execution)
        updated = memory._append_event(
            updated,
            "EXECUTION_REPLAYED",
            f"Wznowiono zatrzymane wykonanie zadania {binding.task_id} bez zmiany bindingu",
            task_id=binding.task_id,
            plan_version=binding.plan_version,
            correlation_id=binding.correlation_id,
        )
        return updated, None

    memory.execution_transaction(transition)


def recover_stalled_launch(
    runtime_root: str,
    project_id: str,
    execution_binding_id: str,
    *,
    apply: bool = False,
    workflow: ProjectWorkflow | None = None,
) -> StalledLaunchRecoveryResult:
    """Validate or explicitly re-project one stalled launch without creating a new binding.

    Dry-run is the default. With ``apply=True`` the task status/cursor is repaired
    for the existing ACTIVE binding and the exact same launch identity is projected
    into the Browser/Native queue again with a fresh transport TTL.
    """

    current = workflow or ProjectWorkflow(runtime_root)
    binding, outbox = _validate_recovery_target(current, project_id, execution_binding_id)
    queued = current.queue.peek()

    if not apply:
        return StalledLaunchRecoveryResult(
            status="READY",
            project_id=project_id,
            task_id=binding.task_id,
            execution_binding_id=binding.execution_binding_id,
            launch_id=binding.launch_id,
            generation=binding.generation,
            outbox_status=outbox.status,
            queue_launch_id=queued.launch_id if queued is not None else None,
            applied=False,
        )

    _reactivate_same_binding(current, project_id, binding)

    try:
        launch = current.publish_outbox_launch(project_id, binding.launch_id)
    except ProjectWorkflowError as exc:
        raise StalledLaunchRecoveryError(exc.code, str(exc)) from exc

    if launch.launch_id != binding.launch_id or launch.execution_binding_id != binding.execution_binding_id:
        _fail("launch_projection_identity_mismatch", "re-published launch identity differs from the stalled binding")

    refreshed = current.execution.launch_outbox_record(project_id, binding.launch_id)
    return StalledLaunchRecoveryResult(
        status="RECOVERED",
        project_id=project_id,
        task_id=binding.task_id,
        execution_binding_id=binding.execution_binding_id,
        launch_id=binding.launch_id,
        generation=binding.generation,
        outbox_status=refreshed.status if refreshed is not None else outbox.status,
        queue_launch_id=launch.launch_id,
        applied=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely re-project one canonical STALLED BDB launch without creating a new binding.")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--apply", action="store_true", help="Apply recovery. Without this flag the command is read-only.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = recover_stalled_launch(
            args.runtime_root,
            args.project_id,
            args.binding_id,
            apply=args.apply,
        )
    except StalledLaunchRecoveryError as exc:
        print(json.dumps({"status": "ERROR", "code": exc.code, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
