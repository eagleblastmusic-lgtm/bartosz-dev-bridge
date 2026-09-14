"""Explicit recovery from accepted task commits already present in repository history.

Project Memory intentionally does not infer task completion from Git.  This module is a
separate operator-invoked recovery boundary for the case where canonical Project Memory
lags behind repository history.  It is conservative by design: only BDB-shaped accepted
commit messages are recognized, only the contiguous plan prefix can be imported, active
execution bindings fail closed, and repository alignment is fast-forward only.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .project_catalog import ProjectCatalog, ProjectPlan, ProjectRecord
from .project_memory import ProjectMemoryError, ProjectMemoryState, ProjectMemoryStore


RECONCILIATION_SCHEMA = "bdb-project-history-reconciliation-v1"
_MAX_LOG_BYTES = 2 * 1024 * 1024
_MAX_COMMITS = 512
_TASK_ID_RE = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_ACCEPTED_SUBJECT_RE = re.compile(
    rf"^\[skip ci\]\s+(?:feat|fix|test|docs|style|chore|refactor|perf|build|ci):\s+complete\s+(?P<task>{_TASK_ID_RE})(?:\s|$)",
    re.IGNORECASE,
)


class ProjectHistoryReconciliationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProjectHistoryReconciliationError(code, message)


@dataclass(frozen=True)
class AcceptedTaskCommit:
    task_id: str
    commit_sha: str
    subject: str

    def to_dict(self) -> dict[str, str]:
        return {"task_id": self.task_id, "commit_sha": self.commit_sha, "subject": self.subject}


@dataclass(frozen=True)
class ReconciliationPreview:
    project_id: str
    plan_version: str
    repo_path: str
    source_ref: str
    local_branch: str
    local_head: str
    source_head: str
    memory_revision: int
    evidence: tuple[AcceptedTaskCommit, ...]
    importable: tuple[AcceptedTaskCommit, ...]
    already_complete: tuple[str, ...]
    ignored_after_gap: tuple[str, ...]
    first_unreconciled_task: str | None

    @property
    def needs_alignment(self) -> bool:
        return self.local_head != self.source_head

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECONCILIATION_SCHEMA,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "repo_path": self.repo_path,
            "source_ref": self.source_ref,
            "local_branch": self.local_branch,
            "local_head": self.local_head,
            "source_head": self.source_head,
            "needs_alignment": self.needs_alignment,
            "memory_revision": self.memory_revision,
            "evidence": [item.to_dict() for item in self.evidence],
            "importable": [item.to_dict() for item in self.importable],
            "already_complete": list(self.already_complete),
            "ignored_after_gap": list(self.ignored_after_gap),
            "first_unreconciled_task": self.first_unreconciled_task,
        }


@dataclass(frozen=True)
class ReconciliationReceipt:
    preview: ReconciliationPreview
    repo_head_after: str
    imported_task_ids: tuple[str, ...]
    completed_milestones: tuple[str, ...]
    memory_revision_after: int
    idempotent: bool

    def to_dict(self) -> dict[str, Any]:
        value = self.preview.to_dict()
        value.update(
            {
                "status": "NOOP" if self.idempotent else "APPLIED",
                "repo_head_after": self.repo_head_after,
                "imported_task_ids": list(self.imported_task_ids),
                "completed_milestones": list(self.completed_milestones),
                "memory_revision_after": self.memory_revision_after,
                "idempotent": self.idempotent,
            }
        )
        return value


def _git(repo_path: Path, *args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("git_unavailable", f"git operation failed: {exc}")
    if not allow_failure and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        _fail("git_operation_failed", detail[:2_000])
    return completed


def _normalize_github_repo(value: str) -> str | None:
    text = value.strip()
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


def _accepted_body(task_id: str, body: str) -> bool:
    pattern = re.compile(
        rf"(?mi)^\s*{re.escape(task_id)}\s+accepted(?:\s+in\s+milestone\s+run\b|\s*:|\b)"
    )
    return bool(pattern.search(body))


def parse_accepted_task_commits(log_text: str, known_task_ids: Iterable[str]) -> tuple[AcceptedTaskCommit, ...]:
    """Parse only the durable BDB accepted-commit convention from bounded git log text."""

    if len(log_text.encode("utf-8")) > _MAX_LOG_BYTES:
        _fail("history_log_too_large", "repository history observation exceeds the bounded limit")
    known = set(known_task_ids)
    found: dict[str, AcceptedTaskCommit] = {}
    for raw_record in log_text.split("\x1e"):
        record = raw_record.strip("\r\n\x00 ")
        if not record:
            continue
        fields = record.split("\x1f", 2)
        if len(fields) != 3:
            _fail("history_log_invalid", "git history record has an unexpected shape")
        commit_sha, subject, body = (item.strip() for item in fields)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha):
            _fail("history_commit_invalid", "git history contains an invalid commit identity")
        match = _ACCEPTED_SUBJECT_RE.match(subject)
        if match is None:
            continue
        task_id = match.group("task")
        if task_id not in known or not _accepted_body(task_id, body):
            continue
        if task_id in found and found[task_id].commit_sha != commit_sha.lower():
            _fail("ambiguous_task_history", f"multiple accepted commits were found for {task_id}")
        found[task_id] = AcceptedTaskCommit(task_id, commit_sha.lower(), subject)
    return tuple(found[key] for key in sorted(found))


def select_contiguous_reconciliation(
    plan: ProjectPlan,
    execution: Mapping[str, Any],
    evidence: Sequence[AcceptedTaskCommit],
) -> tuple[tuple[AcceptedTaskCommit, ...], tuple[str, ...], tuple[str, ...], str | None]:
    """Select only a contiguous plan prefix; evidence beyond the first gap is ignored."""

    raw_statuses = execution.get("task_statuses", {})
    if not isinstance(raw_statuses, Mapping):
        _fail("project_memory_invalid", "execution.task_statuses is not a mapping")
    evidence_by_task = {item.task_id: item for item in evidence}
    importable: list[AcceptedTaskCommit] = []
    already_complete: list[str] = []
    prefix_ids: set[str] = set()
    first_gap: str | None = None

    for task in plan.tasks:
        current = str(raw_statuses.get(task.task_id, task.status)).lower()
        if current in {"completed", "skipped"}:
            already_complete.append(task.task_id)
            prefix_ids.add(task.task_id)
            continue
        accepted = evidence_by_task.get(task.task_id)
        if accepted is not None:
            importable.append(accepted)
            prefix_ids.add(task.task_id)
            continue
        first_gap = task.task_id
        break

    ignored = tuple(sorted(task_id for task_id in evidence_by_task if task_id not in prefix_ids))
    return tuple(importable), tuple(already_complete), ignored, first_gap


class ProjectHistoryReconciler:
    """Preview and explicitly apply one fail-closed history reconciliation."""

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
            _fail("plan_not_found", "project has no canonical current plan")
        self.repo_path = Path(self.project.local_repo_path).expanduser().absolute()

    def _validate_repo(self) -> None:
        if not self.repo_path.is_dir():
            _fail("repo_not_found", "configured project repository path does not exist")
        top = _git(self.repo_path, "rev-parse", "--show-toplevel").stdout.strip()
        if Path(top).resolve(strict=False) != self.repo_path.resolve(strict=False):
            _fail("repo_path_mismatch", "configured project path is not the repository root")
        dirty = _git(self.repo_path, "status", "--porcelain=v1", "--untracked-files=normal").stdout
        if dirty.strip():
            _fail("repo_dirty", "project repository must be clean before history reconciliation")
        if self.project.github_repo:
            remote = _git(self.repo_path, "remote", "get-url", "origin").stdout.strip()
            normalized = _normalize_github_repo(remote)
            if normalized is None or normalized != self.project.github_repo.casefold():
                _fail("repo_remote_mismatch", "origin does not match the project's canonical GitHub repository")

    @staticmethod
    def _remote_branch(source_ref: str) -> str | None:
        if not source_ref.startswith("origin/"):
            return None
        branch = source_ref[len("origin/"):]
        if not branch or branch.startswith("-") or ".." in branch or "~" in branch or "^" in branch or ":" in branch:
            _fail("source_ref_invalid", "origin source ref is unsafe")
        return branch

    def preview(self, *, source_ref: str = "origin/main", fetch_origin: bool = False) -> ReconciliationPreview:
        self._validate_repo()
        branch = self._remote_branch(source_ref)
        if fetch_origin:
            if branch is None:
                _fail("fetch_ref_invalid", "--fetch-origin requires an origin/<branch> source ref")
            _git(self.repo_path, "fetch", "--no-tags", "origin", branch)

        local_head = _git(self.repo_path, "rev-parse", "HEAD^{commit}").stdout.strip().lower()
        local_branch = _git(self.repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        source_head = _git(self.repo_path, "rev-parse", f"{source_ref}^{{commit}}").stdout.strip().lower()
        ancestry = _git(self.repo_path, "merge-base", "--is-ancestor", local_head, source_head, allow_failure=True)
        if ancestry.returncode == 1:
            _fail("non_fast_forward_history", "local project HEAD is not an ancestor of the reconciliation source")
        if ancestry.returncode not in {0, 1}:
            _fail("git_operation_failed", (ancestry.stderr or ancestry.stdout or "merge-base failed").strip()[:2_000])

        log = _git(
            self.repo_path,
            "log",
            f"--max-count={_MAX_COMMITS}",
            "--format=%H%x1f%s%x1f%b%x1e",
            source_head,
        ).stdout
        evidence = parse_accepted_task_commits(log, (task.task_id for task in self.plan.tasks))
        state = self.memory.read_state()
        execution = state.execution if isinstance(state.execution, Mapping) else {}
        importable, already, ignored, first_gap = select_contiguous_reconciliation(self.plan, execution, evidence)
        return ReconciliationPreview(
            project_id=self.project_id,
            plan_version=str(self.plan.plan_version),
            repo_path=str(self.repo_path),
            source_ref=source_ref,
            local_branch=local_branch,
            local_head=local_head,
            source_head=source_head,
            memory_revision=int(state.revision),
            evidence=evidence,
            importable=importable,
            already_complete=already,
            ignored_after_gap=ignored,
            first_unreconciled_task=first_gap,
        )

    def apply(self, preview: ReconciliationPreview, *, align_repo: bool = True) -> ReconciliationReceipt:
        if preview.project_id != self.project_id or preview.plan_version != str(self.plan.plan_version):
            _fail("stale_preview", "reconciliation preview no longer matches the current project plan")
        self._validate_repo()
        observed_local = _git(self.repo_path, "rev-parse", "HEAD^{commit}").stdout.strip().lower()
        observed_source = _git(self.repo_path, "rev-parse", f"{preview.source_ref}^{{commit}}").stdout.strip().lower()
        if observed_local != preview.local_head or observed_source != preview.source_head:
            _fail("stale_preview", "repository identity changed after reconciliation preview")

        if preview.needs_alignment:
            if not align_repo:
                _fail("repo_alignment_required", "project repository requires explicit fast-forward alignment")
            expected_branch = self._remote_branch(preview.source_ref)
            if expected_branch is None or preview.local_branch != expected_branch:
                _fail("repo_branch_mismatch", "fast-forward alignment requires the matching checked-out local branch")
            _git(self.repo_path, "merge", "--ff-only", preview.source_head)

        head_after = _git(self.repo_path, "rev-parse", "HEAD^{commit}").stdout.strip().lower()
        if head_after != preview.source_head:
            _fail("repo_alignment_failed", "project repository did not reach the reconciliation source HEAD")

        if not preview.importable:
            return ReconciliationReceipt(
                preview=preview,
                repo_head_after=head_after,
                imported_task_ids=(),
                completed_milestones=(),
                memory_revision_after=preview.memory_revision,
                idempotent=True,
            )

        evidence_by_task = {item.task_id: item for item in preview.importable}
        plan_task_ids = {item.task_id for item in self.plan.tasks}

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, tuple[tuple[str, ...], tuple[str, ...]]]:
            execution = dict(state.execution if isinstance(state.execution, Mapping) else {})
            statuses = dict(execution.get("task_statuses", {}))
            bindings = execution.get("bindings", [])
            if not isinstance(bindings, list):
                _fail("project_memory_invalid", "execution.bindings is not a list")

            active_by_task = {
                str(binding.get("task_id"))
                for binding in bindings
                if isinstance(binding, Mapping)
                and str(binding.get("status", "")).upper() == "ACTIVE"
                and not bool(binding.get("superseded", False))
            }
            conflict = sorted(active_by_task & set(evidence_by_task))
            if conflict:
                _fail("active_binding_conflict", f"active execution binding exists for: {', '.join(conflict)}")

            imported: list[str] = []
            updated = state
            for task in self.plan.tasks:
                evidence = evidence_by_task.get(task.task_id)
                if evidence is None:
                    continue
                current = str(statuses.get(task.task_id, task.status)).lower()
                if current in {"completed", "skipped"}:
                    continue
                if current not in {"pending", "active", "review", "blocked"}:
                    _fail("task_status_invalid", f"task {task.task_id} has unsupported status {current}")
                statuses[task.task_id] = "completed"
                imported.append(task.task_id)

            execution["task_statuses"] = statuses
            if str(execution.get("current_task_id") or "") in set(imported):
                execution["current_task_id"] = None
                execution["current_binding_id"] = None
            updated = replace(updated, execution=execution)

            for task_id in imported:
                evidence = evidence_by_task[task_id]
                updated = self.memory._append_event(
                    updated,
                    "TASK_COMPLETED",
                    f"Uzgodniono {task_id} z zaakceptowanym commitem repo {evidence.commit_sha[:12]}",
                    task_id=task_id,
                    plan_version=str(self.plan.plan_version),
                    git_head=evidence.commit_sha,
                )

            execution = dict(updated.execution if isinstance(updated.execution, Mapping) else {})
            statuses = dict(execution.get("task_statuses", {}))
            completed_milestones = set(execution.get("milestones_completed", []))
            newly_completed: list[str] = []
            for milestone in self.plan.milestones:
                required = [task for task in self.plan.tasks if task.milestone_id == milestone.milestone_id]
                if not required:
                    continue
                if all(str(statuses.get(task.task_id, task.status)).lower() in {"completed", "skipped"} for task in required):
                    if milestone.milestone_id not in completed_milestones:
                        completed_milestones.add(milestone.milestone_id)
                        newly_completed.append(milestone.milestone_id)
            execution["milestones_completed"] = sorted(completed_milestones)
            updated = replace(updated, execution=execution)
            for milestone_id in newly_completed:
                updated = self.memory._append_event(
                    updated,
                    "MILESTONE_COMPLETED",
                    f"Milestone {milestone_id} uzgodniono z zaakceptowaną historią repo",
                    milestone_id=milestone_id,
                    plan_version=str(self.plan.plan_version),
                    git_head=head_after,
                )
            return updated, (tuple(imported), tuple(newly_completed))

        try:
            imported, milestones = self.memory.execution_transaction(
                transition,
                expected_revision=preview.memory_revision,
            )
        except ProjectMemoryError as exc:
            raise ProjectHistoryReconciliationError(exc.code, str(exc)) from exc
        final_state = self.memory.read_state()
        return ReconciliationReceipt(
            preview=preview,
            repo_head_after=head_after,
            imported_task_ids=imported,
            completed_milestones=milestones,
            memory_revision_after=int(final_state.revision),
            idempotent=not imported,
        )


__all__ = [
    "AcceptedTaskCommit",
    "ProjectHistoryReconciler",
    "ProjectHistoryReconciliationError",
    "RECONCILIATION_SCHEMA",
    "ReconciliationPreview",
    "ReconciliationReceipt",
    "parse_accepted_task_commits",
    "select_contiguous_reconciliation",
]
