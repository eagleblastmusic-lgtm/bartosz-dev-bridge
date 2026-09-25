"""CLI for explicit, fail-closed canonical task completion invalidation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .composition import default_vnext_runtime_root
from .project_execution import (
    ProjectExecutionCoordinator,
    ProjectExecutionError,
    transitive_dependents,
)
from .project_workflow import ProjectWorkflow, ProjectWorkflowError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or apply canonical task completion invalidation and downstream recovery."
    )
    parser.add_argument("--project-id", required=True, help="Canonical project ID.")
    parser.add_argument("--task-id", required=True, help="Task ID to invalidate completion for.")
    parser.add_argument("--runtime-root", default=str(default_vnext_runtime_root()), help="Runtime root path.")
    parser.add_argument("--attempt-id", default=None, help="Target attempt ID (fail-closed if mismatch).")
    parser.add_argument(
        "--expected-result-digest",
        default=None,
        help="Target result digest (fail-closed if mismatch).",
    )
    parser.add_argument("--reason", required=True, help="Audit reason for invalidating completion.")
    parser.add_argument("--invalidated-by", default="operator", help="Actor recording the invalidation.")
    parser.add_argument("--apply", action="store_true", help="Apply the invalidation mutation.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --apply; confirms the explicit recovery mutation.",
    )
    return parser


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.apply and not args.yes:
        _print(
            {
                "schema": "bdb-completion-invalidation-receipt-v1",
                "status": "FAILED",
                "error_code": "explicit_confirmation_required",
                "error": "--apply requires --yes",
            }
        )
        return 2

    runtime_root = Path(args.runtime_root)
    try:
        coordinator = ProjectExecutionCoordinator(str(runtime_root))
        if not args.apply:
            # Preview mode: validate project, task, attempt, and plan without mutating
            project, plan, memory = coordinator._project(args.project_id)
            state = memory.read_state()
            task = next((item for item in plan.tasks if item.task_id == args.task_id), None)
            if task is None:
                raise ProjectExecutionError("task_not_found", f"task '{args.task_id}' does not exist in current plan")
            task_status = state.execution.get("task_statuses", {}).get(args.task_id, task.status)
            if task_status not in {"completed", "skipped"}:
                raise ProjectExecutionError(
                    "task_not_completed",
                    f"task '{args.task_id}' has status '{task_status}' and cannot be invalidated",
                )
            # Find attempts for this task
            task_attempts = [
                a for a in state.execution.get("attempts", [])
                if a.get("task_id") == args.task_id and a.get("result_status") == "PASS"
            ]
            target_attempt = None
            if args.attempt_id:
                target_attempt = next((a for a in task_attempts if a.get("attempt_id") == args.attempt_id), None)
                if target_attempt is None:
                    raise ProjectExecutionError(
                        "attempt_id_mismatch",
                        f"attempt '{args.attempt_id}' not found among accepted attempts for task '{args.task_id}'",
                    )
            elif task_attempts:
                target_attempt = task_attempts[-1]

            if target_attempt and args.expected_result_digest:
                if target_attempt.get("result_digest") != args.expected_result_digest:
                    raise ProjectExecutionError(
                        "result_digest_mismatch",
                        f"attempt digest '{target_attempt.get('result_digest')}' does not match expected '{args.expected_result_digest}'",
                    )

            # Determine downstream tasks via deterministic transitive dependency traversal
            downstream_tasks = transitive_dependents(plan.tasks, args.task_id)

            _print(
                {
                    "schema": "bdb-completion-invalidation-preview-v1",
                    "status": "PREVIEW",
                    "project_id": args.project_id,
                    "task_id": args.task_id,
                    "target_attempt_id": target_attempt.get("attempt_id") if target_attempt else None,
                    "target_result_digest": target_attempt.get("result_digest") if target_attempt else None,
                    "current_task_status": task_status,
                    "projected_task_status": "active",
                    "downstream_candidate_tasks": downstream_tasks,
                    "reason": args.reason,
                    "hint": "Run with --apply --yes to execute invalidation",
                }
            )
            return 0

        # Apply mode
        workflow = ProjectWorkflow(runtime_root)
        receipt = workflow.invalidate_task_completion(
            args.project_id,
            args.task_id,
            attempt_id=args.attempt_id,
            expected_result_digest=args.expected_result_digest,
            reason=args.reason,
            invalidated_by=args.invalidated_by,
        )
        _print(
            {
                **receipt,
                "status": "APPLIED",
                "invalidation_status": receipt.get("status"),
            }
        )
        return 0
    except (ProjectExecutionError, ProjectWorkflowError) as exc:
        _print(
            {
                "schema": "bdb-completion-invalidation-receipt-v1",
                "status": "FAILED",
                "error_code": exc.code,
                "error": str(exc),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
