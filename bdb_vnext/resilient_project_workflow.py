"""Fail-closed recovery around ProjectWorkflow execution handoffs.

This compatibility layer fixes lifecycle gaps without weakening the immutable
execution binding contract:

* an explicitly blocked milestone task may be retried with a fresh binding for
  the same task/plan/run after the previous binding reached a terminal state;
* a blocked task may also be recovered from its terminal execution evidence when
  the legacy active milestone-run projection is stale or already completed;
* after an accepted promoted result, the registered local checkout is advanced
  by a clean fast-forward to that exact accepted HEAD before AUTO may bind the
  next task.

The core ProjectExecutionCoordinator remains the authority for bindings and
results.  This class only supplies a stricter task selection/repository
alignment boundary around ProjectWorkflow.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from .project_execution import ProjectExecutionError
from .project_workflow import ProjectWorkflow, ProjectWorkflowError


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def _normalize_github_repo(value: str) -> str | None:
    text = value.strip().rstrip("/")
    patterns = (
        r"^https://github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
        r"^git@github\.com:(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group("repo").removesuffix(".git").casefold()
    return None


def _task_by_id(plan: Any, task_id: str) -> Any | None:
    return next((task for task in getattr(plan, "tasks", ()) if getattr(task, "task_id", None) == task_id), None)


class ResilientProjectWorkflow(ProjectWorkflow):
    """ProjectWorkflow with bounded retry and post-promotion checkout alignment."""

    def _blocked_retry_task_id(self, project_id: str) -> str | None:
        memory = self.memory(project_id)
        plan = memory.current_plan()
        if plan is None:
            raise ProjectWorkflowError("project_plan_required", "Project Plan must be imported before retry")
        state = memory.read_state()
        execution = state.execution if isinstance(state.execution, Mapping) else {}
        statuses = execution.get("task_statuses", {})
        if not isinstance(statuses, Mapping):
            statuses = {}

        run = execution.get("active_milestone_run")
        run_status = str(run.get("status") or "").lower() if isinstance(run, Mapping) else ""
        raw_task_id: str | None = None

        if run_status == "blocked":
            run_task_id = run.get("current_task_id") if isinstance(run, Mapping) else None
            if not isinstance(run_task_id, str) or not run_task_id:
                raise ProjectWorkflowError("execution_recovery_task_missing", "blocked milestone run has no retry task")
            task = _task_by_id(plan, run_task_id)
            if task is None:
                raise ProjectWorkflowError("execution_recovery_task_missing", "blocked milestone retry task is not in the current plan")
            if str(getattr(task, "milestone_id", "")) != str(run.get("milestone_id") or ""):
                raise ProjectWorkflowError("execution_recovery_ambiguous", "blocked retry task is outside the active milestone")
            raw_task_id = run_task_id
        else:
            # Live migrated projects can retain a legacy/completed active-run
            # projection while the canonical task status and terminal attempt
            # already identify the real blocked task. Recover only when that
            # evidence is unique and the stale run is not active.
            attempts = execution.get("attempts", [])
            candidates: list[str] = []
            if isinstance(attempts, list):
                for attempt in reversed(attempts):
                    if not isinstance(attempt, Mapping):
                        continue
                    task_id = attempt.get("task_id")
                    if not isinstance(task_id, str) or not task_id or task_id in candidates:
                        continue
                    if str(attempt.get("plan_version") or plan.plan_version) != str(plan.plan_version):
                        continue
                    task = _task_by_id(plan, task_id)
                    if task is None:
                        continue
                    status = str(statuses.get(task_id, getattr(task, "status", "pending"))).lower()
                    if status != "blocked":
                        continue
                    result_status = str(attempt.get("result_status") or "").upper()
                    execution_status = str(attempt.get("execution_status") or "").upper()
                    if result_status != "FAIL" and execution_status not in {"FAIL", "FAILED", "BLOCKED"}:
                        continue
                    candidates.append(task_id)

            if not candidates:
                return None
            if run_status in {"running", "review"}:
                raise ProjectWorkflowError(
                    "execution_recovery_ambiguous",
                    "active milestone run disagrees with blocked retry evidence",
                )
            if len(candidates) != 1:
                raise ProjectWorkflowError(
                    "execution_recovery_ambiguous",
                    "multiple terminal blocked tasks are eligible for retry",
                )
            raw_task_id = candidates[0]

        task = _task_by_id(plan, raw_task_id)
        if task is None:
            raise ProjectWorkflowError("execution_recovery_task_missing", "blocked retry task is not in the current plan")
        status = str(statuses.get(raw_task_id, getattr(task, "status", "pending"))).lower()
        if status == "review":
            raise ProjectWorkflowError("execution_review_required", "review state cannot be reopened as an automatic retry")
        if status != "blocked":
            raise ProjectWorkflowError("execution_recovery_ambiguous", "blocked retry subject disagrees with task status")

        cursor = execution.get("current_task_id")
        if isinstance(cursor, str) and cursor and cursor != raw_task_id:
            if _task_by_id(plan, cursor) is not None:
                raise ProjectWorkflowError("execution_recovery_ambiguous", "canonical task cursor disagrees with blocked retry task")
            # A stale pointer to a task no longer present in this plan must not
            # defeat the stronger terminal retry evidence.

        try:
            active = self.execution.current_task_binding(project_id, raw_task_id)
        except ProjectExecutionError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc
        if active is not None:
            raise ProjectWorkflowError("execution_retry_binding_active", "blocked retry already has an active execution binding")
        return raw_task_id

    def queue_continue_prompt(self, project_id: str):  # type: ignore[override]
        """Queue Continue, selecting a terminally blocked task explicitly.

        Normal runnable tasks use the original ProjectWorkflow path unchanged.
        A blocked retry always receives a new generation binding whose expected
        HEAD is observed after the previous terminal attempt.
        """

        retry_task_id = self._blocked_retry_task_id(project_id)
        if retry_task_id is None:
            return super().queue_continue_prompt(project_id)
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        head = self.current_repo_head(project)
        try:
            binding = self.execution.new_binding(
                project_id,
                task_id=retry_task_id,
                expected_repo_head_before=head,
            )
        except ProjectExecutionError as exc:
            raise ProjectWorkflowError(exc.code, str(exc)) from exc
        return self._queue_execution_prompt(project_id, "continue", binding_override=binding)

    def _accepted_head_after(self, project_id: str, completed_task_id: str) -> str | None:
        snapshot = self.execution.snapshot(project_id)
        attempts = snapshot.get("attempts", [])
        if not isinstance(attempts, list):
            return None
        for raw in reversed(attempts):
            if not isinstance(raw, Mapping):
                continue
            if raw.get("task_id") != completed_task_id or str(raw.get("result_status") or "").upper() != "PASS":
                continue
            head = raw.get("head_after")
            if isinstance(head, str) and _SHA_RE.fullmatch(head.lower()):
                return head.lower()
        return None

    def _safe_fast_forward_to_accepted_head(self, project_id: str, expected_head: str) -> str:
        """Advance the configured project checkout to one exact accepted HEAD.

        No reset, force, checkout, rebase, or merge commit is permitted.  A dirty
        worktree, detached/unsafe branch, wrong origin, remote mismatch, or
        non-fast-forward relation fails closed before the checkout is changed.
        """

        if _SHA_RE.fullmatch(expected_head) is None:
            raise ProjectWorkflowError("repo_alignment_head_invalid", "accepted repository HEAD is invalid")
        project = self.catalog.get(project_id)
        if project is None:
            raise ProjectWorkflowError("project_not_found", "project is not in the canonical catalog")
        repo = Path(project.local_repo_path).expanduser().absolute()
        if not repo.is_dir():
            raise ProjectWorkflowError("repo_alignment_missing", "registered project checkout is missing")

        def run(*args: str):
            try:
                return self.runner.run(("git", *args), cwd=repo, timeout_seconds=60)
            except Exception as exc:  # subprocess timeout/OS errors remain fail-closed
                raise ProjectWorkflowError("repo_alignment_git_failed", str(exc)) from exc

        dirty = run("status", "--porcelain=v1", "--untracked-files=normal")
        if dirty.returncode != 0:
            raise ProjectWorkflowError("repo_alignment_git_failed", dirty.stderr.strip() or "git status failed")
        if dirty.stdout.strip():
            raise ProjectWorkflowError("repo_alignment_dirty", "project checkout is dirty; automatic fast-forward is disabled")

        branch_result = run("rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or branch == "HEAD" or _SAFE_BRANCH_RE.fullmatch(branch) is None or ".." in branch or branch.startswith("-"):
            raise ProjectWorkflowError("repo_alignment_branch_invalid", "project checkout is detached or branch name is unsafe")

        if project.github_repo:
            remote_result = run("remote", "get-url", "origin")
            normalized = _normalize_github_repo(remote_result.stdout.strip()) if remote_result.returncode == 0 else None
            if normalized is None or normalized != str(project.github_repo).casefold():
                raise ProjectWorkflowError("repo_alignment_origin_mismatch", "origin does not match the configured GitHub repository")

        fetched = run("fetch", "--no-tags", "origin", branch)
        if fetched.returncode != 0:
            raise ProjectWorkflowError("repo_alignment_fetch_failed", fetched.stderr.strip() or "git fetch failed")
        remote_head_result = run("rev-parse", f"origin/{branch}^{{commit}}")
        remote_head = remote_head_result.stdout.strip().lower()
        if remote_head_result.returncode != 0 or remote_head != expected_head:
            raise ProjectWorkflowError("repo_alignment_remote_mismatch", "origin branch does not equal the accepted promoted HEAD")

        local_result = run("rev-parse", "HEAD^{commit}")
        local_head = local_result.stdout.strip().lower()
        if local_result.returncode != 0 or _SHA_RE.fullmatch(local_head) is None:
            raise ProjectWorkflowError("repo_alignment_head_unavailable", "local repository HEAD could not be read")
        if local_head == expected_head:
            return expected_head

        ancestry = run("merge-base", "--is-ancestor", local_head, expected_head)
        if ancestry.returncode != 0:
            raise ProjectWorkflowError("repo_alignment_non_fast_forward", "local HEAD is not an ancestor of the accepted promoted HEAD")
        merged = run("merge", "--ff-only", expected_head)
        if merged.returncode != 0:
            raise ProjectWorkflowError("repo_alignment_fast_forward_failed", merged.stderr.strip() or "fast-forward failed")
        after = run("rev-parse", "HEAD^{commit}")
        if after.returncode != 0 or after.stdout.strip().lower() != expected_head:
            raise ProjectWorkflowError("repo_alignment_verification_failed", "project checkout did not reach the accepted promoted HEAD")
        return expected_head

    def _ensure_auto_next_launch(self, project_id: str, *, completed_task_id: str | None = None):  # type: ignore[override]
        """Align a promoted checkout before the inherited AUTO next-launch path.

        Alignment failure never invalidates the already accepted task.  It only
        suppresses automatic creation of a potentially stale next binding; the
        operator can repair/synchronize the checkout and continue explicitly.
        """

        if completed_task_id is not None:
            accepted_head = self._accepted_head_after(project_id, completed_task_id)
            if accepted_head is not None:
                try:
                    self._safe_fast_forward_to_accepted_head(project_id, accepted_head)
                except ProjectWorkflowError as exc:
                    return None, f"repo_alignment_required:{exc.code}"
        return super()._ensure_auto_next_launch(project_id, completed_task_id=completed_task_id)


__all__ = ["ResilientProjectWorkflow"]
