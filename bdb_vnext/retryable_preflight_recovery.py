"""Bounded recovery for non-mutating preflight failures in project AUTO.

This module exists for one narrow class of failures: an execution binding was
created against an old repository HEAD, the model stopped before validation or
promotion, and the operator subsequently fast-forwarded the registered local
checkout to the observed upstream HEAD.

Recovery is explicit and fail-closed. It preserves the failed attempt and
binding as immutable history, reopens only the same task, and reconciles both
the v1 Project Memory task/milestone projection and the v2 AUTO scope cursor.
It never resets Git, rewrites history, force-checks out, or fabricates a PASS.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn

from .project_catalog import ProjectCatalog
from .project_memory import ProjectMemoryState, ProjectMemoryStore
from .project_memory_v2_store import ProjectMemoryStoreV2


RECOVERY_SCHEMA = "bdb-retryable-preflight-recovery-v1"
_RETRYABLE_FAILURE_CODES = frozenset({"HEAD_MISMATCH", "REPO_HEAD_MISMATCH"})
_ALLOWED_EXECUTION_STATUSES = frozenset({"BLOCKED", "FAIL", "FAILED"})
_ALLOWED_VALIDATION_STATUSES = frozenset({"NOT_RUN", "NOT_ATTEMPTED"})
_ALLOWED_PROMOTION_STATUSES = frozenset({"NOT_RUN", "NOT_ATTEMPTED"})
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class RetryablePreflightRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise RetryablePreflightRecoveryError(code, message)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_failure_code(value: object) -> str:
    text = str(value or "").strip().upper().replace("-", "_")
    return text


def is_retryable_preflight_attempt(attempt: Mapping[str, Any]) -> bool:
    """Return True only for the narrow, no-validation/no-promotion HEAD mismatch shape."""

    return (
        str(attempt.get("result_status") or "").upper() == "FAIL"
        and normalize_failure_code(attempt.get("failure_code")) in _RETRYABLE_FAILURE_CODES
        and str(attempt.get("execution_status") or "").upper() in _ALLOWED_EXECUTION_STATUSES
        and str(attempt.get("validation_status") or "").upper() in _ALLOWED_VALIDATION_STATUSES
        and str(attempt.get("promotion_status") or "").upper() in _ALLOWED_PROMOTION_STATUSES
    )


def _git(repo: Path, *args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RetryablePreflightRecoveryError("git_unavailable", "git operation failed") from exc
    if not allow_failure and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        _fail("git_operation_failed", detail[:2_000])
    return result


def _head(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if _SHA_RE.fullmatch(text) is None:
        _fail("recovery_head_invalid", f"{field} is not an exact Git object identity")
    return text


def _execution(state: ProjectMemoryState) -> dict[str, Any]:
    if not isinstance(state.execution, Mapping):
        _fail("project_memory_invalid", "execution state is unavailable")
    return dict(state.execution)


@dataclass(frozen=True)
class RetryablePreflightPreview:
    project_id: str
    plan_version: str
    task_id: str
    attempt_id: str
    execution_binding_id: str
    failed_head_before: str
    recovered_head: str
    upstream_ref: str
    v1_revision: int
    v1_task_status: str
    v1_run_status: str | None
    v2_state_revision: int
    v2_status: str
    v2_disposition: str
    v2_run_id: str
    v2_scope: str
    v2_milestone_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_SCHEMA,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "execution_binding_id": self.execution_binding_id,
            "failed_head_before": self.failed_head_before,
            "recovered_head": self.recovered_head,
            "upstream_ref": self.upstream_ref,
            "v1_revision": self.v1_revision,
            "v1_task_status": self.v1_task_status,
            "v1_run_status": self.v1_run_status,
            "v2_state_revision": self.v2_state_revision,
            "v2_status": self.v2_status,
            "v2_disposition": self.v2_disposition,
            "v2_run_id": self.v2_run_id,
            "v2_scope": self.v2_scope,
            "v2_milestone_id": self.v2_milestone_id,
        }


@dataclass(frozen=True)
class RetryablePreflightReceipt:
    preview: RetryablePreflightPreview
    v1_revision_after: int
    v2_state_revision_after: int
    v1_changed: bool
    v2_changed: bool

    def to_dict(self) -> dict[str, Any]:
        value = self.preview.to_dict()
        value.update(
            {
                "status": "APPLIED" if self.v1_changed or self.v2_changed else "NOOP",
                "v1_revision_after": self.v1_revision_after,
                "v2_state_revision_after": self.v2_state_revision_after,
                "v1_changed": self.v1_changed,
                "v2_changed": self.v2_changed,
            }
        )
        return value


class RetryablePreflightRecovery:
    def __init__(self, runtime_root: str | Path, project_id: str) -> None:
        self.runtime_root = Path(runtime_root).expanduser().absolute()
        self.project_id = project_id
        self.catalog = ProjectCatalog(self.runtime_root)
        self.project = self.catalog.get(project_id)
        if self.project is None:
            _fail("project_not_found", "project is not in the canonical catalog")
        self.memory = ProjectMemoryStore(self.runtime_root, project_id)
        self.plan = self.memory.current_plan()
        if self.plan is None:
            _fail("project_plan_required", "project has no canonical plan")
        self.repo = Path(self.project.local_repo_path).expanduser().absolute()
        self.v2_path = self.runtime_root / "control" / "project-memory-v2" / f"{project_id}.db"

    def _repo_identity(self, *, fetch_upstream: bool) -> tuple[str, str]:
        if not self.repo.is_dir():
            _fail("repo_not_found", "registered project repository does not exist")
        top = Path(_git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=False)
        if top != self.repo.resolve(strict=False):
            _fail("repo_path_mismatch", "registered project path is not the repository root")
        dirty = _git(self.repo, "status", "--porcelain=v1", "--untracked-files=normal").stdout.strip()
        if dirty:
            _fail("repo_dirty", "project repository must be clean before recovery")

        upstream_result = _git(
            self.repo,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
            allow_failure=True,
        )
        if upstream_result.returncode != 0 or not upstream_result.stdout.strip():
            _fail("repo_upstream_missing", "checked-out project branch has no configured upstream")
        upstream = upstream_result.stdout.strip()
        if fetch_upstream:
            if "/" not in upstream:
                _fail("repo_upstream_invalid", "configured upstream is not a remote-tracking branch")
            remote, branch = upstream.split("/", 1)
            if not remote or not branch or branch.startswith("-") or ".." in branch:
                _fail("repo_upstream_invalid", "configured upstream is unsafe")
            _git(self.repo, "fetch", "--no-tags", remote, branch)

        local_head = _head(_git(self.repo, "rev-parse", "HEAD^{commit}").stdout, "local HEAD")
        upstream_head = _head(_git(self.repo, "rev-parse", f"{upstream}^{{commit}}").stdout, "upstream HEAD")
        if local_head != upstream_head:
            _fail("repo_not_aligned", "local project HEAD must equal its configured upstream before recovery")
        return local_head, upstream

    def _v1_candidate(self, recovered_head: str) -> tuple[ProjectMemoryState, str, Mapping[str, Any], Mapping[str, Any], str | None]:
        state = self.memory.read_state()
        execution = _execution(state)
        task_id = str(execution.get("current_task_id") or "")
        if not task_id:
            _fail("recovery_task_missing", "canonical current task is unavailable")
        statuses = execution.get("task_statuses", {})
        if not isinstance(statuses, Mapping):
            _fail("project_memory_invalid", "task statuses are unavailable")
        task_status = str(statuses.get(task_id) or "").lower()
        if task_status not in {"blocked", "active"}:
            _fail("recovery_task_not_blocked", "current task is not a recoverable blocked/active task")

        attempts = execution.get("attempts", [])
        if not isinstance(attempts, list):
            _fail("project_memory_invalid", "execution attempts are unavailable")
        attempt = next(
            (
                item
                for item in reversed(attempts)
                if isinstance(item, Mapping)
                and str(item.get("task_id")) == task_id
                and is_retryable_preflight_attempt(item)
            ),
            None,
        )
        if attempt is None:
            _fail("retryable_preflight_not_found", "no retryable HEAD-mismatch preflight attempt exists for the current task")
        attempt_id = str(attempt.get("attempt_id") or "")
        binding_id = str(attempt.get("execution_binding_id") or "")
        bindings = execution.get("bindings", [])
        if not isinstance(bindings, list):
            _fail("project_memory_invalid", "execution bindings are unavailable")
        binding = next(
            (item for item in bindings if isinstance(item, Mapping) and str(item.get("execution_binding_id")) == binding_id),
            None,
        )
        if binding is None:
            _fail("recovery_binding_missing", "failed execution binding is unavailable")
        if str(binding.get("task_id")) != task_id or str(binding.get("status") or "").upper() != "FAILED":
            _fail("recovery_binding_not_failed", "retryable preflight binding is not terminal FAILED")
        failed_head = _head(binding.get("expected_repo_head_before"), "binding expected HEAD")
        if failed_head == recovered_head:
            _fail("recovery_head_unchanged", "repository HEAD still equals the failed binding HEAD")
        attempt_head_after = attempt.get("head_after")
        if attempt_head_after is None:
            _fail("recovery_target_missing", "retryable attempt did not report the observed canonical HEAD")
        if _head(attempt_head_after, "attempt head_after") != recovered_head:
            _fail("recovery_target_mismatch", "local/upstream HEAD does not match the canonical HEAD observed by the failed attempt")

        active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
        run_status = str(active_run.get("status")) if active_run is not None else None
        if active_run is not None:
            if str(active_run.get("current_task_id") or task_id) != task_id:
                _fail("recovery_run_task_mismatch", "active milestone run points at another task")
            if run_status not in {"blocked", "running"}:
                _fail("recovery_run_not_blocked", "active milestone run is not recoverable")
        return state, task_id, attempt, binding, run_status

    def _v2_candidate(self, task_id: str) -> Mapping[str, Any]:
        if not self.v2_path.is_file():
            _fail("v2_state_missing", "canonical Project Memory v2 database is unavailable")
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        store.initialize()
        with store._connect() as conn:  # read-only observation through the store's configured connection
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute("SELECT * FROM scope_cursors WHERE project_id = ?", (self.project_id,)).fetchone()
            if row is None:
                _fail("v2_cursor_missing", "AUTO scope cursor is unavailable")
            value = dict(row)
        current_task = str(value.get("current_task_id") or "")
        if current_task != task_id:
            _fail("v2_task_mismatch", "AUTO cursor points at another task")
        status = str(value.get("status") or "ACTIVE")
        disposition = str(value.get("disposition") or "ACTIVE")
        if status not in {"BLOCKED", "ACTIVE"} and disposition not in {"BLOCKED", "HALT_BLOCKED", "ACTIVE"}:
            _fail("v2_cursor_not_recoverable", "AUTO cursor is not in a recoverable blocked/active state")
        if not value.get("run_id") or not value.get("scope") or not value.get("current_milestone_id"):
            _fail("v2_cursor_invalid", "AUTO cursor identity is incomplete")
        return value

    def preview(self, *, fetch_upstream: bool = False) -> RetryablePreflightPreview:
        recovered_head, upstream = self._repo_identity(fetch_upstream=fetch_upstream)
        state, task_id, attempt, binding, run_status = self._v1_candidate(recovered_head)
        v2 = self._v2_candidate(task_id)
        return RetryablePreflightPreview(
            project_id=self.project_id,
            plan_version=str(self.plan.plan_version),
            task_id=task_id,
            attempt_id=str(attempt.get("attempt_id")),
            execution_binding_id=str(binding.get("execution_binding_id")),
            failed_head_before=_head(binding.get("expected_repo_head_before"), "binding expected HEAD"),
            recovered_head=recovered_head,
            upstream_ref=upstream,
            v1_revision=int(state.revision),
            v1_task_status=str(state.execution.get("task_statuses", {}).get(task_id) or ""),
            v1_run_status=run_status,
            v2_state_revision=int(v2.get("state_revision") or 0),
            v2_status=str(v2.get("status") or "ACTIVE"),
            v2_disposition=str(v2.get("disposition") or "ACTIVE"),
            v2_run_id=str(v2.get("run_id")),
            v2_scope=str(v2.get("scope")),
            v2_milestone_id=str(v2.get("current_milestone_id")),
        )

    def _apply_v1(self, preview: RetryablePreflightPreview) -> tuple[int, bool]:
        changed = {"value": False}

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution(state)
            task_id = preview.task_id
            attempts = execution.get("attempts", [])
            matching_attempt = next(
                (
                    item
                    for item in attempts
                    if isinstance(item, Mapping)
                    and str(item.get("attempt_id")) == preview.attempt_id
                    and str(item.get("execution_binding_id")) == preview.execution_binding_id
                    and is_retryable_preflight_attempt(item)
                ),
                None,
            )
            if matching_attempt is None:
                _fail("stale_recovery_preview", "retryable attempt changed before v1 recovery")
            if _head(matching_attempt.get("head_after"), "attempt head_after") != preview.recovered_head:
                _fail("stale_recovery_preview", "retryable attempt target changed before v1 recovery")

            statuses = dict(execution.get("task_statuses", {}))
            current_status = str(statuses.get(task_id) or "").lower()
            if current_status == "active":
                if execution.get("current_binding_id") not in (None, ""):
                    _fail("recovery_active_binding_conflict", "an active current binding already exists")
                return state, None
            if current_status != "blocked":
                _fail("stale_recovery_preview", "task status changed before v1 recovery")

            statuses[task_id] = "active"
            execution["task_statuses"] = statuses
            execution["current_task_id"] = task_id
            execution["current_binding_id"] = None

            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active_run is not None:
                run_id = str(active_run.get("milestone_run_id") or "")
                if not run_id:
                    _fail("recovery_run_invalid", "active milestone run has no identity")
                runs = dict(execution.get("milestone_runs", {}))
                run = dict(runs.get(run_id, active_run))
                if str(run.get("current_task_id") or task_id) != task_id:
                    _fail("recovery_run_task_mismatch", "active milestone run changed before recovery")
                run.update(
                    {
                        "status": "running",
                        "progress_status": "RUNNABLE",
                        "current_task_id": task_id,
                        "blocker": None,
                        "updated_at": _now_iso(),
                    }
                )
                runs[run_id] = run
                execution["milestone_runs"] = runs
                execution["active_milestone_run"] = run

            updated = replace(state, execution=execution)
            updated = self.memory._append_event(
                updated,
                "EXECUTION_RETRYABLE_PREFLIGHT_RECOVERED",
                f"Wznowiono {task_id} po bezpiecznej synchronizacji HEAD do {preview.recovered_head}",
                task_id=task_id,
                plan_version=str(self.plan.plan_version),
            )
            changed["value"] = True
            return updated, None

        self.memory.execution_transaction(transition)
        return int(self.memory.read_state().revision), bool(changed["value"])

    def _apply_v2(self, preview: RetryablePreflightPreview) -> tuple[int, bool]:
        store = ProjectMemoryStoreV2(self.runtime_root, self.project_id)
        store.initialize()
        changed = False
        revision_after = preview.v2_state_revision
        with store._transaction() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute("SELECT * FROM scope_cursors WHERE project_id = ?", (self.project_id,)).fetchone()
            if row is None:
                _fail("v2_cursor_missing", "AUTO scope cursor disappeared before recovery")
            value = dict(row)
            if str(value.get("run_id")) != preview.v2_run_id or str(value.get("current_task_id") or "") != preview.task_id:
                _fail("stale_recovery_preview", "AUTO cursor identity changed before v2 recovery")
            status = str(value.get("status") or "ACTIVE")
            disposition = str(value.get("disposition") or "ACTIVE")
            if status == "ACTIVE" and disposition == "ACTIVE":
                return int(value.get("state_revision") or 0), False
            if status != "BLOCKED" and disposition not in {"BLOCKED", "HALT_BLOCKED"}:
                _fail("stale_recovery_preview", "AUTO cursor is no longer blocked in the expected way")

            expected_revision = int(value.get("state_revision") or 0)
            revision_after = expected_revision + 1
            explanation = json.dumps(
                {
                    "reason_code": "RETRYABLE_PREFLIGHT_RECOVERED",
                    "explanation": f"Recovered {preview.task_id} after local/upstream HEAD alignment.",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            result = conn.execute(
                """
                UPDATE scope_cursors
                   SET status = 'ACTIVE', disposition = 'ACTIVE', state_revision = ?,
                       explanation_json = ?, updated_at = ?
                 WHERE project_id = ? AND state_revision = ?
                   AND current_task_id = ?
                """,
                (
                    revision_after,
                    explanation,
                    _now_iso(),
                    self.project_id,
                    expected_revision,
                    preview.task_id,
                ),
            )
            if result.rowcount != 1:
                _fail("stale_recovery_preview", "AUTO cursor changed during recovery")
            conn.execute(
                "UPDATE runs SET status = 'running', current_task_id = ?, finished_at = NULL WHERE run_id = ? AND project_id = ?",
                (preview.task_id, preview.v2_run_id, self.project_id),
            )
            conn.execute(
                "UPDATE scopes SET status = 'RUNNING', finished_at = NULL WHERE scope_id = ? AND project_id = ?",
                (f"scope:{preview.v2_run_id}", self.project_id),
            )
            changed = True
        return revision_after, changed

    def apply(self, preview: RetryablePreflightPreview) -> RetryablePreflightReceipt:
        if preview.project_id != self.project_id or preview.plan_version != str(self.plan.plan_version):
            _fail("stale_recovery_preview", "recovery preview does not match the current project plan")
        current = self.preview(fetch_upstream=False)
        immutable_fields = (
            "task_id",
            "attempt_id",
            "execution_binding_id",
            "failed_head_before",
            "recovered_head",
            "upstream_ref",
            "v2_run_id",
            "v2_scope",
            "v2_milestone_id",
        )
        if any(getattr(current, name) != getattr(preview, name) for name in immutable_fields):
            _fail("stale_recovery_preview", "canonical recovery subject changed after preview")

        v1_revision_after, v1_changed = self._apply_v1(preview)
        v2_revision_after, v2_changed = self._apply_v2(preview)
        return RetryablePreflightReceipt(
            preview=preview,
            v1_revision_after=v1_revision_after,
            v2_state_revision_after=v2_revision_after,
            v1_changed=v1_changed,
            v2_changed=v2_changed,
        )


__all__ = [
    "RECOVERY_SCHEMA",
    "RetryablePreflightPreview",
    "RetryablePreflightReceipt",
    "RetryablePreflightRecovery",
    "RetryablePreflightRecoveryError",
    "is_retryable_preflight_attempt",
    "normalize_failure_code",
]
