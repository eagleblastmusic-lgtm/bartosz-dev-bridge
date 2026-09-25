"""Canonical project execution binding and bounded result projection.

This module deliberately does not execute commands.  The existing BDB command,
Work Kernel, Candidate, Evidence and promotion authorities remain responsible
for execution.  It binds their machine-readable result to one Project Memory
task and applies one idempotent, stale-safe project transition.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from bdb_shared.evidence import semantic_digest

from .binding_lifecycle import (
    BINDING_STATUS_VALUES,
    BindingLifecycleError,
    STATUS_ACCEPTED,
    STATUS_ACTIVE,
    STATUS_FAILED,
    STATUS_SUPERSEDED,
    check_binding_lifecycle_invariants,
    reconcile_execution_bindings,
    validate_binding_transition,
)
from .project_catalog import ProjectCatalog, ProjectPlan, ProjectRecord, ProjectTask
from .project_memory import ProjectMemoryState, ProjectMemoryStore, available_project_tasks, milestone_auto_progress, task_prerequisite_blockers
from .result_identity import (
    CURRENT_IDENTITY_VERSION,
    IDENTITY_VERSION_V1,
    IDENTITY_VERSION_V2,
    execution_result_digest,
    execution_result_digest_v1,
    execution_result_digest_v2,
    result_identity_v1,
    result_identity_v2,
    verify_result_digest,
)


PROJECT_EXECUTION_SCHEMA = "bdb-project-execution-v1"
PROJECT_EXECUTION_SUBMISSION_SCHEMA = "bdb-project-execution-submission-v1"
PROJECT_EXECUTION_CHECKPOINT_SCHEMA = "bdb-project-execution-checkpoint-v1"
PROJECT_LAUNCH_HANDOFF_SCHEMA = "bdb-project-launch-handoff-v1"
PROJECT_LAUNCH_OUTBOX_SCHEMA = "bdb-project-launch-outbox-v1"
OUTBOX_STATUS_PENDING = "PENDING"
OUTBOX_STATUS_PUBLISHED = "PUBLISHED"
OUTBOX_STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
OUTBOX_STATUS_VALUES = frozenset({OUTBOX_STATUS_PENDING, OUTBOX_STATUS_PUBLISHED, OUTBOX_STATUS_ACKNOWLEDGED})
RESULT_STATUS_SUCCESS = frozenset({"PASS", "SUCCEEDED", "SUCCESS"})
RESULT_STATUS_FAILURE = frozenset({"FAIL", "FAILED", "BLOCKED", "REVIEW_REQUIRED"})
RESULT_STATUS_NON_TERMINAL = frozenset({"WAITING_EXTERNAL", "PENDING", "RUNNING", "VALIDATING", "AWAITING_CI", "UNKNOWN"})
PROMOTION_STATUS_SUCCESS = RESULT_STATUS_SUCCESS | frozenset({"PROMOTED"})
PROMOTION_STATUS_FAILURE = RESULT_STATUS_FAILURE | frozenset({"NOT_RUN", "SKIPPED"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEAD_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONVERSATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_REPO_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ProjectExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise ProjectExecutionError(code, message, details=details)


def _text(value: object, field: str, *, max_length: int = 512, required: bool = True) -> str:
    if not isinstance(value, str):
        _fail("execution_field_invalid", f"{field} must be text")
    value = value.strip()
    if required and not value:
        _fail("execution_field_invalid", f"{field} must not be empty")
    if len(value) > max_length:
        _fail("execution_field_too_large", f"{field} exceeds its bound")
    return value


def _identifier(value: object, field: str) -> str:
    value = _text(value, field, max_length=128)
    if _ID_RE.fullmatch(value) is None:
        _fail("execution_identity_invalid", f"{field} has an unsafe format")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_checkpoint_time(value: object) -> datetime:
    text = _text(value, "last_progress_at", max_length=64)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        _fail("checkpoint_time_invalid", "last_progress_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        _fail("checkpoint_time_invalid", "last_progress_at must include timezone")
    return parsed.astimezone(timezone.utc)


def _status(value: object, field: str) -> str:
    value = _text(value, field, max_length=32).upper()
    return value


def _final_result_status(value: object, field: str) -> str:
    """Only completed execution results may cross the final-result boundary."""
    status = _status(value, field)
    success = PROMOTION_STATUS_SUCCESS if field == "promotion_status" else RESULT_STATUS_SUCCESS
    failure = PROMOTION_STATUS_FAILURE if field == "promotion_status" else RESULT_STATUS_FAILURE
    if status in success or status in failure:
        return status
    if status in RESULT_STATUS_NON_TERMINAL:
        _fail("execution_result_non_terminal", f"{field}={status} is intermediate; keep this binding active until validation finishes")
    _fail("execution_status_invalid", f"{field}={status} is not a supported final status")


def _head(value: object, field: str, *, allow_unknown: bool = False) -> str | None:
    if value is None and not allow_unknown:
        _fail("execution_field_invalid", f"{field} is required")
    if value is None:
        return None
    text = _text(value, field, max_length=128)
    if allow_unknown and text == "unknown":
        return text
    if _HEAD_RE.fullmatch(text.lower()) is None:
        _fail("repo_head_invalid", f"{field} is not a Git object identity")
    return text.lower()


def _conversation(value: object, field: str = "conversation_id") -> str:
    text = _text(value, field, max_length=128)
    if _CONVERSATION_RE.fullmatch(text) is None:
        _fail("execution_conversation_invalid", f"{field} has an unsafe format")
    return text


@dataclass(frozen=True)
class ProjectExecutionSubmission:
    """Strict machine result emitted by Work for one canonical launch binding."""

    project_id: str
    plan_version: str
    task_id: str
    execution_binding_id: str
    correlation_id: str
    command_id: str
    repo_alias: str
    head_before: str
    head_after: str | None
    execution_status: str
    validation_status: str
    promotion_status: str
    result_summary: str
    evidence_refs: tuple[str, ...] = ()
    criteria: tuple[Mapping[str, Any], ...] = ()
    canonical_refs: Mapping[str, Any] | None = None
    failure_code: str | None = None
    schema: str = PROJECT_EXECUTION_SUBMISSION_SCHEMA

    @classmethod
    def from_mapping(cls, value: object) -> "ProjectExecutionSubmission":
        if not isinstance(value, Mapping) or value.get("schema") != PROJECT_EXECUTION_SUBMISSION_SCHEMA:
            _fail("execution_schema_invalid", "project execution result schema differs")
        required = {
            "schema", "project_id", "plan_version", "task_id", "execution_binding_id",
            "correlation_id", "command_id", "repo_alias", "head_before", "head_after",
            "execution_status", "validation_status", "promotion_status", "result_summary",
            "evidence_refs", "criteria",
        }
        missing = sorted(required - set(value))
        if missing:
            _fail("execution_field_required", f"project execution result is missing: {', '.join(missing)}")
        allowed = {
            "schema", "project_id", "plan_version", "task_id", "execution_binding_id",
            "correlation_id", "command_id", "repo_alias", "head_before", "head_after",
            "execution_status", "validation_status", "promotion_status", "result_summary",
            "evidence_refs", "criteria", "canonical_refs", "failure_code",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            _fail("execution_field_unknown", f"project execution result contains unsupported fields: {', '.join(unknown)}")
        refs = value.get("evidence_refs", [])
        if not isinstance(refs, list) or len(refs) > 128:
            _fail("execution_shape_invalid", "evidence_refs must be a bounded list")
        criteria = value.get("criteria", [])
        if not isinstance(criteria, list) or len(criteria) > 128 or any(not isinstance(item, Mapping) for item in criteria):
            _fail("execution_shape_invalid", "criteria must be a bounded list of objects")
        normalized_criteria: list[Mapping[str, Any]] = []
        for item in criteria:
            extra = sorted(set(item) - {"criterion", "type", "status", "evidence_ref"})
            if extra:
                _fail("execution_field_unknown", f"criteria contains unsupported fields: {', '.join(extra)}")
            if "criterion" not in item:
                _fail("execution_field_invalid", "criteria[].criterion is required")
            normalized: dict[str, Any] = {
                "criterion": _text(item.get("criterion"), "criteria[].criterion", max_length=2_000),
            }
            for field in ("type", "status"):
                if field in item:
                    normalized[field] = _text(item.get(field), f"criteria[].{field}", max_length=32)
            if "evidence_ref" in item:
                evidence_ref = item.get("evidence_ref")
                normalized["evidence_ref"] = None if evidence_ref is None else _text(evidence_ref, "criteria[].evidence_ref", max_length=512)
            normalized_criteria.append(normalized)
        raw_refs = value.get("canonical_refs")
        canonical_refs: dict[str, Any] | None = None
        if raw_refs is not None:
            if not isinstance(raw_refs, Mapping):
                _fail("execution_shape_invalid", "canonical_refs must be an object")
            allowed_refs = {"task_id", "work_id", "candidate_id", "candidate_view_id", "candidate_tree_digest", "base_commit_oid", "validation_id", "evidence_id", "evaluation_id", "publication_id"}
            extra_refs = sorted(set(raw_refs) - allowed_refs)
            if extra_refs:
                _fail("execution_field_unknown", f"canonical_refs contains unsupported fields: {', '.join(extra_refs)}")
            canonical_refs = {}
            for key, raw_ref in raw_refs.items():
                canonical_refs[key] = None if raw_ref is None else _text(raw_ref, f"canonical_refs.{key}", max_length=128)
        failure = value.get("failure_code")
        if failure is not None:
            failure = _text(failure, "failure_code", max_length=128)
        repo_alias = _text(value.get("repo_alias"), "repo_alias", max_length=64)
        if _REPO_ALIAS_RE.fullmatch(repo_alias) is None:
            _fail("repo_alias_invalid", "repo_alias has an unsafe format")
        return cls(
            project_id=_identifier(value.get("project_id"), "project_id"),
            plan_version=_text(value.get("plan_version"), "plan_version", max_length=32),
            task_id=_identifier(value.get("task_id"), "task_id"),
            execution_binding_id=_identifier(value.get("execution_binding_id"), "execution_binding_id"),
            correlation_id=_identifier(value.get("correlation_id"), "correlation_id"),
            command_id=_identifier(value.get("command_id"), "command_id"),
            repo_alias=repo_alias,
            head_before=_head(value.get("head_before"), "head_before", allow_unknown=True) or "unknown",
            head_after=_head(value.get("head_after"), "head_after", allow_unknown=True),
            execution_status=_final_result_status(value.get("execution_status"), "execution_status"),
            validation_status=_final_result_status(value.get("validation_status"), "validation_status"),
            promotion_status=_final_result_status(value.get("promotion_status"), "promotion_status"),
            result_summary=_text(value.get("result_summary", ""), "result_summary", max_length=4_000, required=False),
            evidence_refs=tuple(_text(item, "evidence_refs[]", max_length=512) for item in refs),
            criteria=tuple(normalized_criteria),
            canonical_refs=canonical_refs,
            failure_code=failure,
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "execution_binding_id": self.execution_binding_id,
            "correlation_id": self.correlation_id,
            "command_id": self.command_id,
            "repo_alias": self.repo_alias,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "execution_status": self.execution_status,
            "validation_status": self.validation_status,
            "promotion_status": self.promotion_status,
            "result_summary": self.result_summary,
            "evidence_refs": list(self.evidence_refs),
            "criteria": [dict(item) for item in self.criteria],
        }
        if self.canonical_refs is not None:
            value["canonical_refs"] = dict(self.canonical_refs)
        if self.failure_code is not None:
            value["failure_code"] = self.failure_code
        return value


def _result_identity(binding: "ProjectExecutionBinding", result: Mapping[str, Any]) -> dict[str, Any]:
    return result_identity_v2(binding, result)


def _execution_document(state: ProjectMemoryState) -> dict[str, Any]:
    raw = state.execution if isinstance(state.execution, Mapping) else {}
    if not raw:
        return {"schema": PROJECT_EXECUTION_SCHEMA, "bindings": [], "attempts": [], "acceptance_results": [], "checkpoints": {}, "task_statuses": {}, "gate_statuses": {}, "open_question_statuses": {}, "milestones_completed": [], "milestone_runs": {}, "launch_handoffs": {}, "launch_outbox": {}, "completion_invalidations": []}
    if raw.get("schema", PROJECT_EXECUTION_SCHEMA) != PROJECT_EXECUTION_SCHEMA:
        _fail("execution_schema_invalid", "project execution state schema differs")
    result = dict(raw)
    result.setdefault("bindings", [])
    result.setdefault("attempts", [])
    result.setdefault("acceptance_results", [])
    result.setdefault("checkpoints", {})
    result.setdefault("task_statuses", {})
    result.setdefault("gate_statuses", {})
    result.setdefault("open_question_statuses", {})
    result.setdefault("milestones_completed", [])
    result.setdefault("milestone_runs", {})
    result.setdefault("launch_handoffs", {})
    result.setdefault("launch_outbox", {})
    result.setdefault("completion_invalidations", [])
    for key in ("bindings", "attempts", "acceptance_results", "completion_invalidations"):
        if not isinstance(result[key], list) or len(result[key]) > 512 or any(not isinstance(item, Mapping) for item in result[key]):
            _fail("execution_shape_invalid", f"execution.{key} is invalid")
    if not isinstance(result["task_statuses"], Mapping) or len(result["task_statuses"]) > 2_048:
        _fail("execution_shape_invalid", "execution.task_statuses is invalid")
    if not isinstance(result["milestone_runs"], Mapping) or len(result["milestone_runs"]) > 128:
        _fail("execution_shape_invalid", "execution.milestone_runs is invalid")
    if not isinstance(result["checkpoints"], Mapping) or len(result["checkpoints"]) > 512:
        _fail("execution_shape_invalid", "execution.checkpoints is invalid")
    for key in ("gate_statuses", "open_question_statuses", "launch_handoffs", "launch_outbox"):
        if not isinstance(result[key], Mapping) or len(result[key]) > 512:
            _fail("execution_shape_invalid", f"execution.{key} is invalid")
    return result


COMPLETION_INVALIDATION_SCHEMA = "bdb-completion-invalidation-receipt-v1"

_NON_CODE_DELIVERABLE_PATTERNS = (
    r"\b(?:validation|verification|test|smoke test|performance|audit|review)\s+(?:record|report|summary)\b",
    r"\b(?:pass/fail|qa)\s+(?:record|report)\b",
    r"\b(?:approved|release)\s+checklist\b",
    r"\b(?:manual\s+test\s+record|screen-reader\s+manual\s+test)\b",
    r"\bvalidation-only\b",
    r"\bno-op\b",
)


_CODE_FILE_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".css", ".cxx", ".go", ".h", ".hpp",
    ".html", ".htm", ".java", ".js", ".json", ".jsx", ".kt", ".kts", ".less",
    ".mjs", ".php", ".ps1", ".py", ".pyw", ".rb", ".rs", ".sass",
    ".scss", ".sh", ".sql", ".svelte", ".swift", ".toml", ".ts",
    ".tsx", ".vue", ".yaml", ".yml",
})


def _has_code_file_indicator(deliverable: str) -> bool:
    clean = deliverable.strip()
    lowered = clean.lower()
    for ext in _CODE_FILE_EXTENSIONS:
        if lowered.endswith(ext):
            return True
    if "/" in clean or "\\" in clean:
        parts = re.split(r"[/\\]", clean)
        if any(part.lower() in {"src", "lib", "app", "components", "pkg", "core", "test", "tests", "dist"} for part in parts):
            if not lowered.endswith((".md", ".txt", ".rst", ".doc", ".pdf", ".png", ".jpg", ".jpeg")):
                return True
    return False


def _explicit_code_deliverable_paths(deliverables: Sequence[str]) -> tuple[str, ...]:
    extensions = "|".join(re.escape(extension) for extension in sorted(_CODE_FILE_EXTENSIONS, key=len, reverse=True))
    file_path = re.compile(
        rf"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+[/\\])*[A-Za-z0-9_.-]+(?:{extensions})(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    paths: list[str] = []
    for deliverable in deliverables:
        clean = deliverable.strip().replace("\\", "/")
        if not clean:
            continue
        paths.extend(match.group(0).removeprefix("./") for match in file_path.finditer(clean))
        if not any(char.isspace() for char in clean) and "/" in clean and _has_code_file_indicator(clean):
            directory = clean.removeprefix("./").strip("/")
            if directory and directory not in paths:
                paths.append(directory)
    return tuple(dict.fromkeys(path for path in paths if path))


def task_requires_code_delivery(task: ProjectTask) -> bool:
    """Determine whether a task requires material code delivery in the repository.

    A task requires code delivery if:
    - Any deliverable declares a code file (by extension, path separator, or source folder).
    - Any deliverable is not strictly a recognized non-code audit/validation/checklist record.
    - Criteria descriptions (even if manual/review) DO NOT waive code delivery when code deliverables are declared.

    A task does NOT require code delivery only if:
    - It has no deliverables AND its acceptance criteria are purely manual/review/external/unknown.
    - OR all declared deliverables are strictly non-code records/reports/checklists and none declare code file paths.
    """
    if not task.deliverables:
        return False

    # Any code indicator in any deliverable mandates code delivery
    if any(_has_code_file_indicator(d) for d in task.deliverables):
        return True

    # If all deliverables match recognized non-code deliverable patterns, no code delivery is required
    for item in task.deliverables:
        lowered = item.strip().lower()
        if not any(re.search(pat, lowered) for pat in _NON_CODE_DELIVERABLE_PATTERNS):
            return True

    return False


def transitive_dependents(tasks: Sequence[ProjectTask], target_task_id: str) -> list[str]:
    """Deterministically calculate all tasks that depend directly or transitively on target_task_id.

    Independent tasks occurring later in the plan list are strictly excluded.
    Returns task IDs in their canonical plan order.
    """
    dependents_map: dict[str, set[str]] = {}
    for t in tasks:
        for dep in t.dependencies:
            dependents_map.setdefault(dep, set()).add(t.task_id)

    downstream: set[str] = set()
    queue = list(dependents_map.get(target_task_id, set()))
    while queue:
        curr = queue.pop(0)
        if curr not in downstream:
            downstream.add(curr)
            queue.extend(dependents_map.get(curr, set()) - downstream)

    return [t.task_id for t in tasks if t.task_id in downstream]


def _run_git(repo_dir: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(repo_dir), *args],
            returncode=128,
            stdout="",
            stderr=str(exc),
        )


def _verify_git_code_delivery(
    local_repo_path: Path,
    *,
    head_before: str | None,
    head_after: str | None,
    task: ProjectTask | None,
) -> tuple[bool, str | None]:
    if not local_repo_path.is_dir() or not (local_repo_path / ".git").exists():
        return False, "repo_not_a_git_repository"

    if not head_after or not isinstance(head_after, str) or len(head_after.strip()) < 7:
        return False, "invalid_or_missing_head_after"

    ha = head_after.strip()

    cat_after = _run_git(local_repo_path, ["cat-file", "-e", f"{ha}^{{commit}}"])
    if cat_after.returncode != 0:
        return False, f"head_after_not_found_in_git:{ha}"

    if head_before:
        hb = head_before.strip()
        if ha.lower() == hb.lower():
            return False, "head_not_advanced"

        cat_before = _run_git(local_repo_path, ["cat-file", "-e", f"{hb}^{{commit}}"])
        if cat_before.returncode != 0:
            return False, f"head_before_not_found_in_git:{hb}"

        anc = _run_git(local_repo_path, ["merge-base", "--is-ancestor", hb, ha])
        if anc.returncode != 0:
            return False, f"head_after_not_ancestor:{hb}..{ha}"

        diff = _run_git(local_repo_path, ["diff", "--name-only", hb, ha])
        if diff.returncode != 0:
            return False, f"git_diff_failed:{hb}..{ha}"
        changed_files = [line.strip().replace("\\", "/") for line in diff.stdout.splitlines() if line.strip()]
        if not changed_files:
            return False, f"empty_git_diff:{hb}..{ha}"

        explicit_paths = _explicit_code_deliverable_paths(task.deliverables) if task else ()
        if explicit_paths:
            def path_matches(declared: str, changed: str) -> bool:
                normalized = declared.removeprefix("./").strip("/")
                if Path(normalized).suffix.lower() in _CODE_FILE_EXTENSIONS:
                    return changed == normalized
                return changed == normalized or changed.startswith(normalized + "/")

            if not any(path_matches(declared, changed) for declared in explicit_paths for changed in changed_files):
                return False, f"git_diff_lacks_declared_code_path:{changed_files}"
        elif not any(_has_code_file_indicator(path) for path in changed_files):
            return False, f"git_diff_lacks_code_changes:{changed_files}"

        return True, None

    ls = _run_git(local_repo_path, ["diff-tree", "--no-commit-id", "--name-only", "-r", ha])
    if ls.returncode == 0 and ls.stdout.strip():
        changed = [line.strip().replace("\\", "/") for line in ls.stdout.splitlines() if line.strip()]
        if any(_has_code_file_indicator(f) for f in changed):
            return True, None

    return False, "head_after_unverified"


def _verify_candidate_code_delivery(
    runtime_root: Path | None,
    canonical_refs: Mapping[str, Any] | None,
    task: ProjectTask | None,
) -> tuple[bool, str | None]:
    if not isinstance(canonical_refs, Mapping) or not canonical_refs:
        return False, "canonical_refs_missing"

    cand_id = canonical_refs.get("candidate_id")
    val_id = canonical_refs.get("validation_id")
    tree_digest = canonical_refs.get("candidate_tree_digest")

    if not cand_id and not val_id and not tree_digest:
        return False, "canonical_refs_empty"

    if not task or not task.task_id:
        return False, "candidate_task_binding_missing"

    if not runtime_root:
        return False, "runtime_root_missing_for_candidate_verification"

    control_db = runtime_root / "control" / "control.db"
    if not control_db.is_file():
        flat_db = runtime_root / "control.db"
        if flat_db.is_file():
            control_db = flat_db
        else:
            return False, f"control_db_not_found:{control_db}"

    try:
        conn = sqlite3.connect(f"file:{control_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    except Exception as exc:
        return False, f"control_db_open_failed:{exc}"

    try:
        table_check = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m4b_candidate_effects'"
        ).fetchone()
        if not table_check:
            return False, "m4b_candidate_effects_table_missing"
        candidate_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(m4b_candidate_effects)").fetchall()}
        if not {"candidate_id", "task_id", "state", "observed_tree_digest", "planned_tree_digest"}.issubset(candidate_columns):
            return False, "m4b_candidate_effects_columns_missing"

        cand_str = str(cand_id).strip() if cand_id else None
        if val_id:
            val_str = str(val_id).strip()
            table_check = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='p1_validation_runs'"
            ).fetchone()
            if not table_check:
                return False, "p1_validation_runs_table_missing"
            validation_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(p1_validation_runs)").fetchall()}
            if not {"validation_id", "candidate_id", "status"}.issubset(validation_columns):
                return False, "p1_validation_runs_columns_missing"
            row = conn.execute(
                "SELECT validation_id, candidate_id, status FROM p1_validation_runs WHERE validation_id = ?",
                (val_str,),
            ).fetchone()
            if not row:
                return False, f"validation_id_not_found:{val_str}"
            status = str(row[2]).upper()
            if status != "PASS":
                return False, f"validation_status_not_pass:{val_str}:{status}"
            validation_candidate = str(row[1]).strip() if row[1] is not None else ""
            if not validation_candidate:
                return False, f"validation_candidate_missing:{val_str}"
            if cand_str and cand_str != validation_candidate:
                return False, f"validation_candidate_mismatch:{val_str}:{row[1]}!={cand_id}"
            cand_str = validation_candidate

        if not cand_str and tree_digest:
            td_str = str(tree_digest).strip()
            rows = conn.execute(
                "SELECT candidate_id, task_id, state, observed_tree_digest, planned_tree_digest "
                "FROM m4b_candidate_effects WHERE observed_tree_digest = ? OR planned_tree_digest = ?",
                (td_str, td_str),
            ).fetchall()
            if not rows:
                return False, f"candidate_tree_digest_not_found:{td_str}"
            if len(rows) != 1:
                return False, f"candidate_tree_digest_ambiguous:{td_str}"
            cand_str = str(rows[0][0]).strip()

        if not cand_str:
            return False, "candidate_reference_missing"

        row = conn.execute(
            "SELECT candidate_id, task_id, state, observed_tree_digest, planned_tree_digest "
            "FROM m4b_candidate_effects WHERE candidate_id = ?",
            (cand_str,),
        ).fetchone()
        if not row:
            return False, f"candidate_id_not_found:{cand_str}"
        state = str(row[2]).upper()
        if state not in {"SEALED", "OBSERVED", "APPLIED"}:
            return False, f"candidate_state_invalid:{cand_str}:{state}"
        candidate_task_id = str(row[1]).strip() if row[1] is not None else ""
        if not candidate_task_id or candidate_task_id != task.task_id:
            return False, f"candidate_task_id_mismatch:{cand_str}:{candidate_task_id or 'NULL'}!={task.task_id}"
        if tree_digest and str(tree_digest).strip() not in {row[3], row[4]}:
            return False, f"candidate_tree_digest_mismatch:{cand_str}"

        return True, None
    finally:
        conn.close()


def _verify_promotion_code_delivery(
    runtime_root: Path | None,
    promotion_status: str,
    task: ProjectTask | None,
) -> tuple[bool, str | None]:
    if str(promotion_status).upper() != "PROMOTED":
        return False, "promotion_status_not_promoted"

    if not runtime_root:
        return False, "runtime_root_missing_for_promotion_verification"

    control_db = runtime_root / "control" / "control.db"
    if not control_db.is_file():
        flat_db = runtime_root / "control.db"
        if flat_db.is_file():
            control_db = flat_db
        else:
            return False, f"control_db_not_found_for_promotion:{control_db}"

    try:
        conn = sqlite3.connect(f"file:{control_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
    except Exception as exc:
        return False, f"control_db_open_failed:{exc}"

    try:
        if task is None or not task.task_id:
            return False, "promotion_task_binding_missing"

        def has_table(name: str) -> bool:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None

        # M7c is a policy cutover, not a task completion record.  It can prove
        # promotion only through its exact effect binding to an applied M7a
        # Git effect whose canonical candidate and work belong to this task.
        if all(has_table(name) for name in (
            "m7c_promotion_cutovers", "m7c_promotion_bindings", "m7a_git_effects", "m4b_candidate_effects"
        )):
            required_columns = {
                "m7c_promotion_cutovers": {"flow_id", "flow_revision_id", "state"},
                "m7c_promotion_bindings": {"effect_id", "flow_id", "flow_revision_id"},
                "m7a_git_effects": {
                    "effect_id", "task_id", "work_id", "candidate_id", "state",
                    "effect_certainty", "observed_ref_oid", "prepared_commit_oid",
                },
                "m4b_candidate_effects": {"candidate_id", "task_id", "work_id"},
            }
            if all(
                columns.issubset({str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()})
                for table, columns in required_columns.items()
            ):
                effects = conn.execute(
                    "SELECT effect_id, work_id, candidate_id, observed_ref_oid, prepared_commit_oid "
                    "FROM m7a_git_effects WHERE task_id=? AND state='AFTER' AND effect_certainty='AFTER'",
                    (task.task_id,),
                ).fetchall()
                for effect_id, work_id, candidate_id, observed_oid, prepared_oid in effects:
                    if not work_id or not candidate_id or not observed_oid or str(observed_oid).lower() != str(prepared_oid).lower():
                        continue
                    binding = conn.execute(
                        "SELECT flow_id, flow_revision_id FROM m7c_promotion_bindings WHERE effect_id=?",
                        (effect_id,),
                    ).fetchone()
                    if not binding or not binding[0] or not binding[1]:
                        continue
                    cutover = conn.execute(
                        "SELECT flow_revision_id FROM m7c_promotion_cutovers "
                        "WHERE flow_id=? AND state='ACTIVE'",
                        (binding[0],),
                    ).fetchone()
                    if not cutover or str(cutover[0]) != str(binding[1]):
                        continue
                    candidate = conn.execute(
                        "SELECT task_id, work_id FROM m4b_candidate_effects WHERE candidate_id=?",
                        (candidate_id,),
                    ).fetchone()
                    if (
                        candidate
                        and candidate[0] is not None
                        and str(candidate[0]).strip() == task.task_id
                        and candidate[1] is not None
                        and str(candidate[1]).strip() == str(work_id)
                    ):
                        return True, None

        return False, "authoritative_promotion_record_not_found"
    finally:
        conn.close()


def verify_authoritative_code_delivery(
    *,
    head_before: str | None = None,
    head_after: str | None = None,
    promotion_status: str = "NOT_RUN",
    canonical_refs: Mapping[str, Any] | None = None,
    local_repo_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    task: ProjectTask | None = None,
) -> tuple[bool, str | None]:
    """Authoritatively verify code delivery fail-closed across three channels:
    1. Git object existence, linear ancestry, and non-empty material code diff.
    2. Authoritative candidate effects and validation runs in control.db.
    3. Task-bound canonical promotion records in control.db.

    Returns (True, None) if verified by any authoritative channel, or (False, reason) if unverified.
    """
    reasons: list[str] = []

    # Channel 1: Git repository check
    if local_repo_path:
        git_ok, git_reason = _verify_git_code_delivery(
            Path(local_repo_path),
            head_before=head_before,
            head_after=head_after,
            task=task,
        )
        if git_ok:
            return True, None
        if git_reason:
            reasons.append(f"git:{git_reason}")

    # Channel 2: Authoritative Candidate & Validation in control.db
    if canonical_refs and isinstance(canonical_refs, Mapping) and any(
        k in canonical_refs for k in ("candidate_id", "validation_id", "candidate_tree_digest")
    ):
        rt = Path(runtime_root) if runtime_root else None
        cand_ok, cand_reason = _verify_candidate_code_delivery(rt, canonical_refs, task)
        if cand_ok:
            return True, None
        if cand_reason:
            reasons.append(f"candidate:{cand_reason}")

    # Channel 3: Authoritative Promotion in control.db
    if str(promotion_status).upper() == "PROMOTED":
        rt = Path(runtime_root) if runtime_root else None
        prom_ok, prom_reason = _verify_promotion_code_delivery(rt, promotion_status, task)
        if prom_ok:
            return True, None
        if prom_reason:
            reasons.append(f"promotion:{prom_reason}")

    reason_summary = "; ".join(reasons) if reasons else "missing_code_deliverable_evidence"
    return False, reason_summary


def has_canonical_code_evidence(
    *,
    head_before: str | None = None,
    head_after: str | None = None,
    promotion_status: str = "NOT_RUN",
    canonical_refs: Mapping[str, Any] | None = None,
    local_repo_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    task: ProjectTask | None = None,
) -> bool:
    """Check whether a submission has authoritative evidence of code deliverables."""
    ok, _ = verify_authoritative_code_delivery(
        head_before=head_before,
        head_after=head_after,
        promotion_status=promotion_status,
        canonical_refs=canonical_refs,
        local_repo_path=local_repo_path,
        runtime_root=runtime_root,
        task=task,
    )
    return ok


@dataclass(frozen=True)
class CompletionInvalidation:
    invalidation_id: str
    project_id: str
    plan_version: str
    task_id: str
    attempt_id: str
    execution_binding_id: str
    result_digest: str
    reason: str
    created_at: str
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invalidation_id": self.invalidation_id,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "execution_binding_id": self.execution_binding_id,
            "result_digest": self.result_digest,
            "reason": self.reason,
            "created_at": self.created_at,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class ProjectExecutionBinding:
    execution_binding_id: str
    project_id: str
    plan_version: str
    task_id: str
    launch_id: str
    correlation_id: str
    command_id: str
    repo_alias: str
    expected_repo_head_before: str
    created_at: str
    status: str = "ACTIVE"
    superseded: bool = False
    generation: int = 1
    conversation_id: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": PROJECT_EXECUTION_SCHEMA,
            "execution_binding_id": self.execution_binding_id,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "launch_id": self.launch_id,
            "correlation_id": self.correlation_id,
            "command_id": self.command_id,
            "repo_alias": self.repo_alias,
            "expected_repo_head_before": self.expected_repo_head_before,
            "created_at": self.created_at,
            "status": self.status,
            "superseded": self.superseded,
            "generation": self.generation,
        }
        if self.conversation_id is not None:
            value["conversation_id"] = self.conversation_id
        if self.finished_at is not None:
            value["finished_at"] = self.finished_at
        return value


@dataclass(frozen=True)
class TaskAcceptanceResult:
    project_id: str
    plan_version: str
    task_id: str
    attempt_id: str
    criteria: tuple[Mapping[str, Any], ...]
    overall: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "bdb-task-acceptance-result-v1", "project_id": self.project_id, "plan_version": self.plan_version, "task_id": self.task_id, "attempt_id": self.attempt_id, "criteria": [dict(item) for item in self.criteria], "overall": self.overall, "created_at": self.created_at}


@dataclass(frozen=True)
class ProjectLaunchOutboxRecord:
    launch_id: str
    execution_binding_id: str
    project_id: str
    plan_version: str
    task_id: str
    correlation_id: str
    command_id: str
    repo_alias: str
    prompt: str
    auto_send: bool
    status: str
    created_at: str
    updated_at: str
    expires_at: str
    expected_repo_head_before: str | None = None
    schema: str = PROJECT_LAUNCH_OUTBOX_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "launch_id": self.launch_id,
            "execution_binding_id": self.execution_binding_id,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "command_id": self.command_id,
            "repo_alias": self.repo_alias,
            "prompt": self.prompt,
            "auto_send": self.auto_send,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }
        if self.expected_repo_head_before is not None:
            value["expected_repo_head_before"] = self.expected_repo_head_before
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProjectLaunchOutboxRecord":
        if not isinstance(value, Mapping) or value.get("schema") != PROJECT_LAUNCH_OUTBOX_SCHEMA:
            _fail("launch_outbox_schema_invalid", "launch outbox schema differs")
        status = _text(value.get("status"), "launch_outbox.status", max_length=16)
        if status not in OUTBOX_STATUS_VALUES:
            _fail("launch_outbox_status_invalid", f"launch outbox status '{status}' is unsupported")
        return cls(
            launch_id=_identifier(value.get("launch_id"), "launch_id"),
            execution_binding_id=_identifier(value.get("execution_binding_id"), "execution_binding_id"),
            project_id=_identifier(value.get("project_id"), "project_id"),
            plan_version=_text(value.get("plan_version"), "plan_version", max_length=32),
            task_id=_identifier(value.get("task_id"), "task_id"),
            correlation_id=_identifier(value.get("correlation_id"), "correlation_id"),
            command_id=_identifier(value.get("command_id"), "command_id"),
            repo_alias=_text(value.get("repo_alias"), "repo_alias", max_length=64),
            prompt=_text(value.get("prompt"), "prompt", max_length=100_000),
            auto_send=bool(value.get("auto_send", False)),
            status=status,
            created_at=_text(value.get("created_at"), "created_at", max_length=64),
            updated_at=_text(value.get("updated_at"), "updated_at", max_length=64),
            expires_at=_text(value.get("expires_at"), "expires_at", max_length=64),
            expected_repo_head_before=value.get("expected_repo_head_before"),
            schema=PROJECT_LAUNCH_OUTBOX_SCHEMA,
        )


@dataclass(frozen=True)
class ProjectExecutionAttempt:
    attempt_id: str
    project_id: str
    plan_version: str
    task_id: str
    execution_binding_id: str
    command_id: str
    started_at: str
    finished_at: str | None
    head_before: str
    head_after: str | None
    execution_status: str
    validation_status: str
    promotion_status: str
    result_status: str
    result_summary: str
    evidence_refs: tuple[str, ...]
    failure_code: str | None
    result_digest: str
    identity_version: str = IDENTITY_VERSION_V2

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "bdb-project-execution-attempt-v1",
            "identity_version": self.identity_version,
            "attempt_id": self.attempt_id,
            "project_id": self.project_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "execution_binding_id": self.execution_binding_id,
            "command_id": self.command_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "execution_status": self.execution_status,
            "validation_status": self.validation_status,
            "promotion_status": self.promotion_status,
            "result_status": self.result_status,
            "result_summary": self.result_summary,
            "evidence_refs": list(self.evidence_refs),
            "failure_code": self.failure_code,
            "result_digest": self.result_digest,
        }


def _binding_from_dict(value: Mapping[str, Any]) -> ProjectExecutionBinding:
    conversation_id = value.get("conversation_id")
    raw_gen = value.get("generation", 1)
    try:
        generation = int(raw_gen)
    except (ValueError, TypeError):
        generation = 1
    return ProjectExecutionBinding(
        _identifier(value.get("execution_binding_id"), "execution_binding_id"),
        _identifier(value.get("project_id"), "project_id"),
        _text(value.get("plan_version"), "plan_version", max_length=32),
        _identifier(value.get("task_id"), "task_id"),
        _identifier(value.get("launch_id"), "launch_id"),
        _identifier(value.get("correlation_id"), "correlation_id"),
        _identifier(value.get("command_id"), "command_id"),
        _text(value.get("repo_alias"), "repo_alias", max_length=64),
        _text(value.get("expected_repo_head_before"), "expected_repo_head_before", max_length=128),
        _text(value.get("created_at"), "created_at", max_length=64),
        _status(value.get("status", STATUS_ACTIVE), "status"),
        bool(value.get("superseded", False)),
        generation=max(1, generation),
        conversation_id=_conversation(conversation_id) if conversation_id is not None else None,
        finished_at=_text(value.get("finished_at"), "finished_at", max_length=64, required=False) if value.get("finished_at") else None,
    )


def _attempt_from_dict(value: Mapping[str, Any]) -> ProjectExecutionAttempt:
    identity_version = str(value.get("identity_version") or IDENTITY_VERSION_V1)
    return ProjectExecutionAttempt(
        _identifier(value.get("attempt_id"), "attempt_id"),
        _identifier(value.get("project_id"), "project_id"),
        _text(value.get("plan_version"), "plan_version", max_length=32),
        _identifier(value.get("task_id"), "task_id"),
        _identifier(value.get("execution_binding_id"), "execution_binding_id"),
        _identifier(value.get("command_id"), "command_id"),
        _text(value.get("started_at"), "started_at", max_length=64),
        value.get("finished_at"),
        _text(value.get("head_before"), "head_before", max_length=128),
        value.get("head_after"),
        _status(value.get("execution_status"), "execution_status"),
        _status(value.get("validation_status"), "validation_status"),
        _status(value.get("promotion_status"), "promotion_status"),
        _status(value.get("result_status"), "result_status"),
        _text(value.get("result_summary", ""), "result_summary", max_length=4_000, required=False),
        tuple(_text(item, "evidence_ref", max_length=512) for item in value.get("evidence_refs", [])),
        value.get("failure_code"),
        _text(value.get("result_digest"), "result_digest", max_length=128),
        identity_version=identity_version,
    )


class ProjectExecutionCoordinator:
    """The sole project execution binding writer."""

    def __init__(self, runtime_root: str, *, catalog: ProjectCatalog | None = None, memory_factory: Any = ProjectMemoryStore) -> None:
        self.runtime_root = runtime_root
        self.catalog = catalog or ProjectCatalog(runtime_root)
        self._memory_factory = memory_factory

    def _project(self, project_id: str) -> tuple[ProjectRecord, ProjectPlan, ProjectMemoryStore]:
        project = self.catalog.get(project_id)
        if project is None:
            _fail("project_not_found", "project is not in the canonical catalog")
        memory = self._memory_factory(self.runtime_root, project_id)
        plan = memory.current_plan()
        if plan is None:
            _fail("project_plan_required", "project execution requires an imported plan")
        return project, plan, memory

    def new_binding(self, project_id: str, *, task_id: str | None = None, expected_repo_head_before: str = "unknown", launch_id: str | None = None, correlation_id: str | None = None, command_id: str | None = None) -> ProjectExecutionBinding:
        project, plan, memory = self._project(project_id)
        state = memory.read_state()
        execution = _execution_document(state)
        selected = task_id or execution.get("current_task_id") or plan.current_task_id
        if selected is None:
            available = available_project_tasks(plan, state)
            if len(available) == 1:
                selected = available[0].task_id
            elif len(available) > 1:
                _fail(
                    "task_selection_required",
                    "multiple execution tasks are runnable; choose one explicitly",
                    details={"task_ids": [item.task_id for item in available]},
                )
        task = next((item for item in plan.tasks if item.task_id == selected), None)
        if task is None:
            _fail("task_not_found", "execution task does not exist")
        statuses = execution.get("task_statuses", {})
        if statuses.get(task.task_id, task.status) in {"completed", "skipped"}:
            _fail("task_already_complete", "completed task cannot start a new binding")
        blockers = task_prerequisite_blockers(plan, state, task)
        if blockers:
            _fail(
                "execution_prerequisites_blocked",
                "task prerequisites are not satisfied",
                details={"task_id": task.task_id, "blocking_dependencies": [dict(item) for item in blockers]},
            )

        task_bindings = [
            b for b in execution.get("bindings", [])
            if b.get("task_id") == task.task_id and str(b.get("plan_version")) == str(plan.plan_version)
        ]
        active_for_task = [b for b in task_bindings if b.get("status") == STATUS_ACTIVE and not b.get("superseded")]
        current_binding_id = execution.get("current_binding_id")
        if current_binding_id and active_for_task:
            current = next((b for b in active_for_task if b.get("execution_binding_id") == current_binding_id), None)
            if current is not None:
                return _binding_from_dict(current)

        max_existing_gen = max((int(b.get("generation", 1)) for b in task_bindings), default=0)
        next_gen = max_existing_gen + 1

        head = _text(expected_repo_head_before, "expected_repo_head_before", max_length=128)
        if head != "unknown" and _HEAD_RE.fullmatch(head) is None:
            _fail("repo_head_invalid", "expected_repo_head_before is not a Git object identity")
        suffix = uuid.uuid4().hex
        return ProjectExecutionBinding(
            f"binding-{suffix}",
            project.project_id,
            plan.plan_version,
            task.task_id,
            launch_id or str(uuid.uuid4()),
            correlation_id or f"corr-{suffix}",
            command_id or f"command-{suffix}",
            project.repo_alias,
            head,
            _utc_now(),
            status=STATUS_ACTIVE,
            superseded=False,
            generation=next_gen,
        )

    def persist_binding(self, binding: ProjectExecutionBinding) -> ProjectExecutionBinding:
        _identifier(binding.project_id, "project_id")
        project, plan, memory = self._project(binding.project_id)
        if binding.plan_version != plan.plan_version or binding.repo_alias != project.repo_alias:
            _fail("execution_binding_stale", "binding no longer matches current project authority")

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionBinding]:
            execution = _execution_document(state)
            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                _fail("task_not_found", "execution task does not exist")
            existing = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding.execution_binding_id), None)
            if existing is not None:
                if semantic_digest(existing) != semantic_digest(binding.to_dict()):
                    _fail("execution_binding_conflict", "binding identity already contains different bytes")
                return state, _binding_from_dict(existing)
            blockers = task_prerequisite_blockers(plan, state, task)
            if blockers:
                _fail(
                    "execution_prerequisites_blocked",
                    "task prerequisites are not satisfied",
                    details={"task_id": task.task_id, "blocking_dependencies": [dict(item) for item in blockers]},
                )
            # Monotonic generation enforcement and atomic supersede of existing active bindings for this task/run
            task_bindings = [
                b for b in execution["bindings"]
                if b.get("task_id") == binding.task_id and str(b.get("plan_version")) == str(binding.plan_version)
            ]
            max_existing_gen = max((int(b.get("generation", 1)) for b in task_bindings), default=0)
            effective_gen = max(binding.generation, max_existing_gen + 1)
            now_iso = _utc_now()

            for b in execution["bindings"]:
                if b.get("task_id") == binding.task_id and str(b.get("plan_version")) == str(binding.plan_version):
                    if b.get("status") == STATUS_ACTIVE and not b.get("superseded"):
                        validate_binding_transition(STATUS_ACTIVE, STATUS_SUPERSEDED)
                        b["status"] = STATUS_SUPERSEDED
                        b["superseded"] = True
                        if not b.get("finished_at"):
                            b["finished_at"] = now_iso

            persisted_binding = replace(binding, generation=effective_gen, status=STATUS_ACTIVE, superseded=False)
            execution["bindings"].append(persisted_binding.to_dict())
            statuses = dict(execution.get("task_statuses", {}))
            statuses.setdefault(binding.task_id, "active")
            execution["task_statuses"] = statuses
            execution["current_task_id"] = binding.task_id
            execution["current_binding_id"] = binding.execution_binding_id

            updated = replace(state, execution=execution)
            updated = memory._append_event(
                updated,
                "EXECUTION_BOUND",
                f"Powiązano wykonanie z zadaniem {binding.task_id} (gen={effective_gen})",
                task_id=binding.task_id,
                plan_version=binding.plan_version,
                correlation_id=binding.correlation_id,
            )
            updated = memory._append_event(
                updated,
                "EXECUTION_STARTED",
                f"Rozpoczęto próbę zadania {binding.task_id} (gen={effective_gen})",
                task_id=binding.task_id,
                plan_version=binding.plan_version,
                correlation_id=binding.correlation_id,
            )
            return updated, persisted_binding
        return memory.execution_transaction(transition)

    def start(self, project_id: str, **kwargs: Any) -> ProjectExecutionBinding:
        return self.persist_binding(self.new_binding(project_id, **kwargs))

    def binding(self, project_id: str, execution_binding_id: str) -> ProjectExecutionBinding:
        """Read one canonical binding without creating or selecting another task."""
        _identifier(project_id, "project_id")
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        raw = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding_id), None)
        if raw is None:
            _fail("execution_binding_not_found", "execution binding does not exist")
        binding = _binding_from_dict(raw)
        if binding.project_id != project_id:
            _fail("execution_binding_stale", "execution binding belongs to another project")
        return binding

    def bind_conversation(self, project_id: str, execution_binding_id: str, conversation_id: str) -> ProjectExecutionBinding:
        """Bind a claimed Browser conversation exactly once to the launch binding."""
        conversation = _conversation(conversation_id)
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        project, plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionBinding]:
            execution = _execution_document(state)
            raw = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding_id), None)
            if raw is None:
                _fail("execution_binding_not_found", "execution binding does not exist")
            current = _binding_from_dict(raw)
            if current.project_id != project.project_id or current.plan_version != plan.plan_version or current.superseded or current.status != "ACTIVE":
                _fail("execution_binding_stale", "execution binding is not active")
            if current.conversation_id not in (None, conversation):
                _fail("execution_conversation_mismatch", "execution binding is owned by another conversation")
            if current.conversation_id == conversation:
                return state, current
            updated_binding = replace(current, conversation_id=conversation)
            bindings = [updated_binding.to_dict() if item.get("execution_binding_id") == binding_id else item for item in execution["bindings"]]
            execution["bindings"] = bindings
            updated = replace(state, execution=execution)
            updated = memory._append_event(updated, "EXECUTION_CONVERSATION_BOUND", f"Powiązano rozmowę z zadaniem {current.task_id}", task_id=current.task_id, plan_version=current.plan_version, correlation_id=current.correlation_id)
            return updated, updated_binding

        return memory.execution_transaction(transition)

    def current_task_binding(self, project_id: str, task_id: str) -> ProjectExecutionBinding | None:
        """Return the one active binding for a task, or fail closed if ambiguous."""
        task = _identifier(task_id, "task_id")
        _project, plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        matches = [
            _binding_from_dict(item)
            for item in execution.get("bindings", [])
            if item.get("project_id") == project_id
            and item.get("plan_version") == plan.plan_version
            and item.get("task_id") == task
            and item.get("status", "ACTIVE") == "ACTIVE"
            and item.get("superseded") is not True
        ]
        if len(matches) > 1:
            _fail("execution_binding_ambiguous", "more than one active binding matches the current task", details={"project_id": project_id, "task_id": task})
        return matches[0] if matches else None

    def restore_current_binding(self, project_id: str, execution_binding_id: str) -> ProjectExecutionBinding:
        """Re-establish the canonical cursor for one already-recorded active binding."""
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        project, plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionBinding]:
            execution = _execution_document(state)
            raw = next((item for item in execution.get("bindings", []) if item.get("execution_binding_id") == binding_id), None)
            if raw is None:
                _fail("execution_binding_not_found", "execution binding does not exist")
            binding = _binding_from_dict(raw)
            if binding.project_id != project_id or binding.plan_version != plan.plan_version or binding.status != "ACTIVE" or binding.superseded:
                _fail("execution_binding_stale", "execution binding is not active")
            if execution.get("current_binding_id") not in (None, binding_id):
                _fail("execution_binding_stale", "another binding is the current canonical binding")
            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                _fail("task_not_found", "execution task does not exist")
            if execution.get("task_statuses", {}).get(binding.task_id, task.status) in {"completed", "skipped"}:
                _fail("task_already_complete", "completed task cannot become the current binding")
            execution["current_task_id"] = binding.task_id
            execution["current_binding_id"] = binding_id
            return replace(state, execution=execution), binding

        return memory.execution_transaction(transition)

    @staticmethod
    def _launch_handoff_from_dict(value: Mapping[str, Any]) -> dict[str, Any]:
        if value.get("schema") != PROJECT_LAUNCH_HANDOFF_SCHEMA:
            _fail("launch_handoff_schema_invalid", "launch handoff schema differs")
        status = _text(value.get("status"), "launch_handoff.status", max_length=16)
        if status not in {"PENDING", "SENT"}:
            _fail("launch_handoff_status_invalid", "launch handoff status is unsupported")
        normalized = {
            "schema": PROJECT_LAUNCH_HANDOFF_SCHEMA,
            "project_id": _identifier(value.get("project_id"), "launch_handoff.project_id"),
            "execution_binding_id": _identifier(value.get("execution_binding_id"), "launch_handoff.execution_binding_id"),
            "task_id": _identifier(value.get("task_id"), "launch_handoff.task_id"),
            "launch_id": _identifier(value.get("launch_id"), "launch_handoff.launch_id"),
            "status": status,
            "updated_at": _text(value.get("updated_at"), "launch_handoff.updated_at", max_length=64),
        }
        conversation = value.get("conversation_id")
        if conversation is not None:
            normalized["conversation_id"] = _conversation(conversation, "launch_handoff.conversation_id")
        return normalized

    def launch_handoff(self, project_id: str, execution_binding_id: str) -> dict[str, Any] | None:
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        raw = execution.get("launch_handoffs", {}).get(binding_id)
        if raw is None:
            return None
        handoff = self._launch_handoff_from_dict(raw)
        if handoff["project_id"] != project_id or handoff["execution_binding_id"] != binding_id:
            _fail("launch_handoff_stale", "launch handoff is bound to another project or binding")
        return handoff

    def mark_launch_handoff_pending(self, project_id: str, binding: ProjectExecutionBinding) -> dict[str, Any]:
        """Durably record that an AUTO launch exists and still needs Send."""
        _project, plan, memory = self._project(project_id)
        if binding.project_id != project_id or binding.plan_version != plan.plan_version:
            _fail("launch_handoff_stale", "launch handoff binding is not current project authority")
        now = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, dict[str, Any]]:
            execution = _execution_document(state)
            handoffs = dict(execution.get("launch_handoffs", {}))
            existing = handoffs.get(binding.execution_binding_id)
            if existing is not None:
                current = self._launch_handoff_from_dict(existing)
                if any(current.get(key) != value for key, value in (("project_id", project_id), ("execution_binding_id", binding.execution_binding_id), ("task_id", binding.task_id), ("launch_id", binding.launch_id))):
                    _fail("launch_handoff_conflict", "launch handoff identity already contains different bytes")
                return state, current
            handoff = {
                "schema": PROJECT_LAUNCH_HANDOFF_SCHEMA,
                "project_id": project_id,
                "execution_binding_id": binding.execution_binding_id,
                "task_id": binding.task_id,
                "launch_id": binding.launch_id,
                "status": "PENDING",
                "updated_at": now,
            }
            handoffs[binding.execution_binding_id] = handoff
            execution["launch_handoffs"] = handoffs
            return replace(state, execution=execution), handoff

        return memory.execution_transaction(transition)

    def mark_launch_handoff_sent(self, project_id: str, *, execution_binding_id: str, launch_id: str, conversation_id: str) -> dict[str, Any]:
        """Commit the post-Send handoff exactly once; never advances task state."""
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        conversation = _conversation(conversation_id)
        _project, plan, memory = self._project(project_id)
        now = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, dict[str, Any]]:
            execution = _execution_document(state)
            handoffs = dict(execution.get("launch_handoffs", {}))
            existing = handoffs.get(binding_id)
            if existing is not None:
                current = self._launch_handoff_from_dict(existing)
                if current["project_id"] != project_id or current["execution_binding_id"] != binding_id or current["launch_id"] != launch_id or current.get("conversation_id") not in (None, conversation):
                    _fail("launch_handoff_conflict", "launch handoff identity does not match the canonical request")
                if current["status"] == "SENT":
                    return state, current
            raw = next((item for item in execution.get("bindings", []) if item.get("execution_binding_id") == binding_id), None)
            if raw is None:
                _fail("execution_binding_not_found", "launch handoff binding does not exist")
            binding = _binding_from_dict(raw)
            if binding.project_id != project_id or binding.plan_version != plan.plan_version or binding.launch_id != launch_id or binding.status != "ACTIVE" or binding.superseded:
                _fail("execution_binding_stale", "launch handoff binding is not active")
            if binding.conversation_id not in (None, conversation):
                _fail("execution_conversation_mismatch", "launch handoff conversation differs")
            if execution.get("current_binding_id") != binding_id:
                _fail("execution_binding_stale", "launch handoff binding is not the current canonical binding")
            handoff = {
                "schema": PROJECT_LAUNCH_HANDOFF_SCHEMA,
                "project_id": project_id,
                "execution_binding_id": binding_id,
                "task_id": binding.task_id,
                "launch_id": launch_id,
                "status": "SENT",
                "conversation_id": conversation,
                "updated_at": now,
            }
            handoffs[binding_id] = handoff
            execution["launch_handoffs"] = handoffs
            return replace(state, execution=execution), handoff

        return memory.execution_transaction(transition)

    def prepare_launch(
        self,
        project_id: str,
        *,
        binding: ProjectExecutionBinding,
        prompt: str,
        auto_send: bool = False,
        ttl_minutes: int = 10,
    ) -> tuple[ProjectExecutionBinding, ProjectLaunchOutboxRecord]:
        """Atomically commit the execution binding and PENDING launch outbox record in one transaction.

        No downstream projection or queue write occurs before this transaction commits.
        """
        _identifier(binding.project_id, "project_id")
        project, plan, memory = self._project(binding.project_id)
        if binding.plan_version != plan.plan_version or binding.repo_alias != project.repo_alias:
            _fail("execution_binding_stale", "binding no longer matches current project authority")

        normalized_prompt = _text(prompt, "prompt", max_length=100_000)
        now_dt = datetime.now(timezone.utc)
        now_iso = _utc_now()
        expires_iso = (now_dt + timedelta(minutes=ttl_minutes)).isoformat(timespec="microseconds").replace("+00:00", "Z")

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, tuple[ProjectExecutionBinding, ProjectLaunchOutboxRecord]]:
            execution = _execution_document(state)
            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                _fail("task_not_found", "execution task does not exist")

            # Check existing binding
            existing = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding.execution_binding_id), None)
            if existing is not None:
                if semantic_digest(existing) != semantic_digest(binding.to_dict()):
                    _fail("execution_binding_conflict", "binding identity already contains different bytes")
                persisted_binding = _binding_from_dict(existing)
            else:
                blockers = task_prerequisite_blockers(plan, state, task)
                if blockers:
                    _fail(
                        "execution_prerequisites_blocked",
                        "task prerequisites are not satisfied",
                        details={"task_id": task.task_id, "blocking_dependencies": [dict(item) for item in blockers]},
                    )
                task_bindings = [
                    b for b in execution["bindings"]
                    if b.get("task_id") == binding.task_id and str(b.get("plan_version")) == str(binding.plan_version)
                ]
                max_existing_gen = max((int(b.get("generation", 1)) for b in task_bindings), default=0)
                effective_gen = max(binding.generation, max_existing_gen + 1)

                for b in execution["bindings"]:
                    if b.get("task_id") == binding.task_id and str(b.get("plan_version")) == str(binding.plan_version):
                        if b.get("status") == STATUS_ACTIVE and not b.get("superseded"):
                            validate_binding_transition(STATUS_ACTIVE, STATUS_SUPERSEDED)
                            b["status"] = STATUS_SUPERSEDED
                            b["superseded"] = True
                            if not b.get("finished_at"):
                                b["finished_at"] = now_iso

                persisted_binding = replace(binding, generation=effective_gen, status=STATUS_ACTIVE, superseded=False)
                execution["bindings"].append(persisted_binding.to_dict())
                statuses = dict(execution.get("task_statuses", {}))
                statuses.setdefault(binding.task_id, "active")
                execution["task_statuses"] = statuses
                execution["current_task_id"] = binding.task_id
                execution["current_binding_id"] = binding.execution_binding_id

            # Prepare Outbox Record (PENDING)
            outbox_dict = dict(execution.get("launch_outbox", {}))
            existing_outbox = outbox_dict.get(binding.launch_id)
            if existing_outbox is not None:
                outbox_rec = ProjectLaunchOutboxRecord.from_dict(existing_outbox)
                if (outbox_rec.project_id != project_id or
                    outbox_rec.execution_binding_id != binding.execution_binding_id or
                    outbox_rec.task_id != binding.task_id):
                    _fail("launch_outbox_conflict", "launch outbox identity already contains different bytes")
            else:
                outbox_rec = ProjectLaunchOutboxRecord(
                    launch_id=binding.launch_id,
                    execution_binding_id=binding.execution_binding_id,
                    project_id=project.project_id,
                    plan_version=str(plan.plan_version),
                    task_id=binding.task_id,
                    correlation_id=binding.correlation_id,
                    command_id=binding.command_id,
                    repo_alias=project.repo_alias,
                    prompt=normalized_prompt,
                    auto_send=auto_send,
                    status=OUTBOX_STATUS_PENDING,
                    created_at=now_iso,
                    updated_at=now_iso,
                    expires_at=expires_iso,
                    expected_repo_head_before=binding.expected_repo_head_before,
                )
                outbox_dict[binding.launch_id] = outbox_rec.to_dict()
                execution["launch_outbox"] = outbox_dict

            # If auto_send, ensure handoffs record exists as well
            if auto_send:
                handoffs = dict(execution.get("launch_handoffs", {}))
                if binding.execution_binding_id not in handoffs:
                    handoffs[binding.execution_binding_id] = {
                        "schema": PROJECT_LAUNCH_HANDOFF_SCHEMA,
                        "project_id": project_id,
                        "execution_binding_id": binding.execution_binding_id,
                        "task_id": binding.task_id,
                        "launch_id": binding.launch_id,
                        "status": "PENDING",
                        "updated_at": now_iso,
                    }
                    execution["launch_handoffs"] = handoffs

            updated = replace(state, execution=execution)
            updated = memory._append_event(
                updated,
                "EXECUTION_BOUND",
                f"Powiązano wykonanie z zadaniem {binding.task_id} (gen={persisted_binding.generation})",
                task_id=binding.task_id,
                plan_version=binding.plan_version,
                correlation_id=binding.correlation_id,
            )
            updated = memory._append_event(
                updated,
                "EXECUTION_STARTED",
                f"Rozpoczęto próbę zadania {binding.task_id} (gen={persisted_binding.generation})",
                task_id=binding.task_id,
                plan_version=binding.plan_version,
                correlation_id=binding.correlation_id,
            )
            return updated, (persisted_binding, outbox_rec)

        return memory.execution_transaction(transition)

    def mark_outbox_published(self, project_id: str, launch_id: str) -> ProjectLaunchOutboxRecord:
        _project, _plan, memory = self._project(project_id)
        now_iso = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectLaunchOutboxRecord]:
            execution = _execution_document(state)
            outbox_dict = dict(execution.get("launch_outbox", {}))
            raw = outbox_dict.get(launch_id)
            if raw is None:
                _fail("launch_outbox_not_found", f"launch outbox record '{launch_id}' does not exist")
            current = ProjectLaunchOutboxRecord.from_dict(raw)
            if current.status == OUTBOX_STATUS_PENDING:
                updated_rec = replace(current, status=OUTBOX_STATUS_PUBLISHED, updated_at=now_iso)
                outbox_dict[launch_id] = updated_rec.to_dict()
                execution["launch_outbox"] = outbox_dict
                return replace(state, execution=execution), updated_rec
            return state, current

        return memory.execution_transaction(transition)

    def mark_outbox_acknowledged(self, project_id: str, launch_id: str, *, conversation_id: str | None = None) -> ProjectLaunchOutboxRecord:
        _project, _plan, memory = self._project(project_id)
        now_iso = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectLaunchOutboxRecord]:
            execution = _execution_document(state)
            outbox_dict = dict(execution.get("launch_outbox", {}))
            raw = outbox_dict.get(launch_id)
            if raw is None:
                _fail("launch_outbox_not_found", f"launch outbox record '{launch_id}' does not exist")
            current = ProjectLaunchOutboxRecord.from_dict(raw)
            if current.status != OUTBOX_STATUS_ACKNOWLEDGED:
                updated_rec = replace(current, status=OUTBOX_STATUS_ACKNOWLEDGED, updated_at=now_iso)
                outbox_dict[launch_id] = updated_rec.to_dict()
                execution["launch_outbox"] = outbox_dict
                return replace(state, execution=execution), updated_rec
            return state, current

        return memory.execution_transaction(transition)

    def rearm_acknowledged_launch(
        self,
        project_id: str,
        launch_id: str,
        *,
        execution_binding_id: str,
        ttl_minutes: int = 10,
    ) -> ProjectLaunchOutboxRecord:
        """Re-arm one acknowledged-but-unfinished launch for explicit operator recovery.

        This is intentionally narrow: the binding must still be the current active
        binding, the task must be non-terminal, no execution result may exist for
        the binding, and the durable handoff must not already be SENT.
        """
        if ttl_minutes <= 0 or ttl_minutes > 60 * 24:
            _fail("launch_rearm_ttl_invalid", "ttl_minutes must be positive and bounded")
        binding_id = _identifier(execution_binding_id, "execution_binding_id")
        _project, plan, memory = self._project(project_id)
        now_dt = datetime.now(timezone.utc)
        now_iso = _utc_now()
        expires_iso = (now_dt + timedelta(minutes=ttl_minutes)).isoformat(timespec="microseconds").replace("+00:00", "Z")

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectLaunchOutboxRecord]:
            execution = _execution_document(state)
            raw_binding = next(
                (
                    item for item in execution.get("bindings", [])
                    if item.get("execution_binding_id") == binding_id
                ),
                None,
            )
            if raw_binding is None:
                _fail("execution_binding_not_found", "launch re-arm binding does not exist")
            binding = _binding_from_dict(raw_binding)
            if (
                binding.project_id != project_id
                or binding.plan_version != plan.plan_version
                or binding.launch_id != launch_id
                or binding.status != STATUS_ACTIVE
                or binding.superseded
            ):
                _fail("execution_binding_stale", "launch re-arm binding is not the current active binding")
            if execution.get("current_binding_id") != binding_id:
                _fail("execution_binding_stale", "launch re-arm binding is not the canonical current binding")

            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                _fail("task_not_found", "launch re-arm task does not exist")
            task_status = str(execution.get("task_statuses", {}).get(binding.task_id, task.status)).lower()
            if task_status in {"completed", "skipped"}:
                _fail("task_already_complete", "completed task cannot be re-armed")

            run = execution.get("active_milestone_run")
            if (
                not isinstance(run, Mapping)
                or run.get("status") != "running"
                or run.get("milestone_id") != task.milestone_id
                or execution.get("current_task_id") != binding.task_id
                or task_status not in {"pending", "active"}
                or task_prerequisite_blockers(plan, state, task)
            ):
                _fail("launch_rearm_not_runnable", "AUTO must be running on this unblocked task before re-arming")

            if any(
                item.get("execution_binding_id") == binding_id
                for item in execution.get("attempts", [])
                if isinstance(item, Mapping)
            ):
                _fail("launch_rearm_result_exists", "execution result already exists for this binding")

            handoff = execution.get("launch_handoffs", {}).get(binding_id)
            if not isinstance(handoff, Mapping):
                _fail("launch_handoff_missing", "canonical launch handoff is missing")
            if handoff.get("status") == "SENT":
                _fail("launch_rearm_already_sent", "canonical launch handoff is already SENT")
            if handoff.get("launch_id") != launch_id or handoff.get("task_id") != binding.task_id:
                _fail("launch_handoff_conflict", "canonical launch handoff identity differs")

            outbox_dict = dict(execution.get("launch_outbox", {}))
            raw_outbox = outbox_dict.get(launch_id)
            if raw_outbox is None:
                _fail("launch_outbox_not_found", f"launch outbox record '{launch_id}' does not exist")
            current = ProjectLaunchOutboxRecord.from_dict(raw_outbox)
            if current.execution_binding_id != binding_id or current.task_id != binding.task_id:
                _fail("launch_outbox_conflict", "launch outbox identity differs from current binding")
            if current.status not in {
                OUTBOX_STATUS_ACKNOWLEDGED,
                OUTBOX_STATUS_PUBLISHED,
                OUTBOX_STATUS_PENDING,
            }:
                _fail("launch_outbox_status_invalid", "launch outbox cannot be re-armed from its current state")

            updated_rec = replace(
                current,
                status=OUTBOX_STATUS_PENDING,
                auto_send=True,
                updated_at=now_iso,
                expires_at=expires_iso,
            )
            outbox_dict[launch_id] = updated_rec.to_dict()
            execution["launch_outbox"] = outbox_dict

            handoffs = dict(execution.get("launch_handoffs", {}))
            handoffs[binding_id] = {
                **dict(handoff),
                "status": "PENDING",
                "updated_at": now_iso,
            }
            execution["launch_handoffs"] = handoffs

            updated = replace(state, execution=execution)
            updated = memory._append_event(
                updated,
                "EXECUTION_LAUNCH_REARMED",
                f"Ponownie uzbrojono handoff zadania {binding.task_id}",
                task_id=binding.task_id,
                plan_version=binding.plan_version,
                correlation_id=binding.correlation_id,
            )
            return updated, updated_rec

        return memory.execution_transaction(transition)

    def launch_outbox_record(self, project_id: str, launch_id: str) -> ProjectLaunchOutboxRecord | None:
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        raw = execution.get("launch_outbox", {}).get(launch_id)
        if raw is None:
            return None
        return ProjectLaunchOutboxRecord.from_dict(raw)

    def pending_outbox_records(self, project_id: str) -> list[ProjectLaunchOutboxRecord]:
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        results = []
        for raw in execution.get("launch_outbox", {}).values():
            rec = ProjectLaunchOutboxRecord.from_dict(raw)
            handoff = execution.get("launch_handoffs", {}).get(rec.execution_binding_id, {})
            if (
                rec.status in {OUTBOX_STATUS_PENDING, OUTBOX_STATUS_PUBLISHED}
                and execution.get("current_binding_id") == rec.execution_binding_id
                and handoff.get("status") != "SENT"
            ):
                results.append(rec)
        return results

    def record_checkpoint(self, project_id: str, execution_binding_id: str, *, status: str, progress_summary: str = "", external_reference: str | None = None, last_progress_at: str | None = None) -> dict[str, Any]:
        binding = self.binding(project_id, execution_binding_id)
        normalized_status = _status(status, "checkpoint_status")
        if normalized_status not in {"ACTIVE", "WAITING_EXTERNAL", "RESUMABLE"}:
            _fail("checkpoint_status_invalid", "checkpoint status is unsupported")
        if external_reference is not None:
            external_reference = _text(external_reference, "external_reference", max_length=512)
        checkpoint = {
            "schema": PROJECT_EXECUTION_CHECKPOINT_SCHEMA,
            "execution_binding_id": binding.execution_binding_id,
            "project_id": binding.project_id,
            "task_id": binding.task_id,
            "plan_version": binding.plan_version,
            "status": normalized_status,
            "progress_summary": _text(progress_summary, "progress_summary", max_length=4_000, required=False),
            "external_reference": external_reference,
            "last_progress_at": _text(last_progress_at or _utc_now(), "last_progress_at", max_length=64),
        }
        _parse_checkpoint_time(checkpoint["last_progress_at"])
        _project, plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, dict[str, Any]]:
            execution = _execution_document(state)
            current = _binding_from_dict(next(item for item in execution["bindings"] if item.get("execution_binding_id") == binding.execution_binding_id))
            if current.status != "ACTIVE" or current.superseded:
                _fail("execution_binding_stale", "checkpoint binding is not active")
            checkpoints = dict(execution.get("checkpoints", {})); checkpoints[binding.execution_binding_id] = checkpoint; execution["checkpoints"] = checkpoints
            updated = replace(state, execution=execution)
            updated = memory._append_event(updated, "EXECUTION_CHECKPOINT", f"Checkpoint {normalized_status} dla {binding.task_id}", task_id=binding.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            return updated, checkpoint

        return memory.execution_transaction(transition)

    def watchdog(self, project_id: str, *, now: datetime | None = None, inactivity_seconds: float = 300.0) -> dict[str, Any]:
        """Return an inactivity projection; it never marks a task failed/completed."""
        _project, _plan, memory = self._project(project_id)
        state = memory.read_state(); execution = _execution_document(state)
        binding_id = execution.get("current_binding_id")
        if not binding_id:
            return {"state": "IDLE", "resume_available": False, "execution_binding_id": None}
        binding = self.binding(project_id, str(binding_id))
        checkpoint = execution.get("checkpoints", {}).get(binding.execution_binding_id, {})
        checkpoint_status = str(checkpoint.get("status") or "ACTIVE")
        last_text = checkpoint.get("last_progress_at") or binding.created_at
        last_at = _parse_checkpoint_time(last_text)
        observed = now or datetime.now(timezone.utc)
        age = max(0.0, (observed.astimezone(timezone.utc) - last_at).total_seconds())
        if checkpoint_status == "WAITING_EXTERNAL":
            state_name = "WAITING_EXTERNAL"
        elif age >= inactivity_seconds:
            state_name = "STALLED"
        else:
            state_name = "ACTIVE"
        return {
            "state": state_name,
            "resume_available": state_name == "STALLED" or checkpoint_status == "RESUMABLE",
            "execution_binding_id": binding.execution_binding_id,
            "task_id": binding.task_id,
            "last_progress_at": last_text,
            "inactivity_seconds": age,
            "external_reference": checkpoint.get("external_reference"),
            "progress_summary": checkpoint.get("progress_summary", ""),
        }

    def resume_binding(self, project_id: str, execution_binding_id: str) -> ProjectExecutionBinding:
        binding = self.binding(project_id, execution_binding_id)
        if binding.status != "ACTIVE" or binding.superseded:
            _fail("execution_binding_stale", "same-binding resume is no longer safe")
        return binding

    def existing_result(self, project_id: str, result: Mapping[str, Any]) -> ProjectExecutionAttempt | None:
        """Find an exact replay before mutation; used only to label receipts."""
        binding = self.binding(project_id, _identifier(result.get("execution_binding_id"), "execution_binding_id"))
        digest_v2 = execution_result_digest_v2(binding, result)
        digest_v1 = execution_result_digest_v1(binding, result)
        execution = _execution_document(self._project(project_id)[2].read_state())
        invalidated_attempt_ids = {
            item["attempt_id"]
            for item in execution.get("completion_invalidations", [])
            if isinstance(item, Mapping) and item.get("attempt_id")
        }
        existing = next(
            (
                item for item in execution["attempts"]
                if item.get("execution_binding_id") == binding.execution_binding_id and (
                    item.get("attempt_id") not in invalidated_attempt_ids
                ) and (
                    item.get("result_digest") == digest_v2 or (
                        item.get("result_digest") == digest_v1 and item.get("identity_version") in (None, IDENTITY_VERSION_V1)
                    )
                )
            ),
            None,
        )
        return _attempt_from_dict(existing) if existing is not None else None

    def begin_milestone_auto(self, project_id: str, *, milestone_id: str | None = None, milestone_run_id: str | None = None) -> dict[str, Any]:
        """Start or resume one canonical milestone run without executing work."""
        project, plan, memory = self._project(project_id)
        initial_state = memory.read_state()
        progress = milestone_auto_progress(plan, initial_state, milestone_id)
        if progress.get("status") not in {"RUNNABLE", "MILESTONE_COMPLETED"}:
            _fail(str(progress.get("status") or "MILESTONE_REQUIRED"), "milestone AUTO cannot start", details=progress)
        selected = str(progress.get("milestone_id"))
        initial_execution = _execution_document(initial_state)
        initial_active = initial_execution.get("active_milestone_run") if isinstance(initial_execution.get("active_milestone_run"), Mapping) else None
        inherited_run_id = initial_active.get("milestone_run_id") if initial_active and initial_active.get("milestone_id") == selected else None
        run_id = _identifier(milestone_run_id or inherited_run_id or f"milestone-run-{uuid.uuid4().hex}", "milestone_run_id")
        now = _utc_now()

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state)
            runs = dict(execution.get("milestone_runs", {}))
            active = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active and active.get("status") in {"running", "review", "blocked"} and active.get("milestone_id") != selected:
                _fail("milestone_run_active", "another milestone AUTO run is already active")
            existing = runs.get(run_id)
            if existing is not None and (existing.get("milestone_id") != selected or existing.get("project_id") != project_id):
                _fail("milestone_run_conflict", "milestone run identity is already bound to another milestone")
            run = {
                **(dict(existing) if isinstance(existing, Mapping) else {}),
                "schema": "bdb-milestone-run-v1",
                "milestone_run_id": run_id,
                "project_id": project_id,
                "plan_version": plan.plan_version,
                "milestone_id": selected,
                "status": "completed" if progress.get("status") == "MILESTONE_COMPLETED" else "running",
                "started_at": (existing or {}).get("started_at", now),
                "updated_at": now,
                "current_task_id": progress.get("next_task_id"),
            }
            runs[run_id] = run
            execution["milestone_runs"] = runs
            execution["active_milestone_run"] = run
            execution["current_task_id"] = progress.get("next_task_id")
            if existing is None:
                execution["current_binding_id"] = None
            updated = replace(state, execution=execution)
            if existing is None:
                updated = memory._append_event(updated, "MILESTONE_AUTO_STARTED", f"Uruchomiono AUTO dla milestone {selected}", milestone_id=selected, plan_version=plan.plan_version)
            return updated, None

        memory.execution_transaction(transition)
        return self.milestone_auto_snapshot(project_id, run_id=run_id)

    def stop_milestone_auto(self, project_id: str, *, run_id: str, reason: str = "stopped_by_user") -> dict[str, Any]:
        project, plan, memory = self._project(project_id)
        run_id = _identifier(run_id, "milestone_run_id")

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state)
            runs = dict(execution.get("milestone_runs", {}))
            run = runs.get(run_id)
            if not isinstance(run, Mapping):
                _fail("milestone_run_not_found", "milestone run does not exist")
            updated_run = {**dict(run), "status": "stopped", "stop_reason": _text(reason, "stop_reason", max_length=256), "updated_at": _utc_now()}
            runs[run_id] = updated_run
            execution["milestone_runs"] = runs
            if isinstance(execution.get("active_milestone_run"), Mapping) and execution["active_milestone_run"].get("milestone_run_id") == run_id:
                execution["active_milestone_run"] = updated_run
            updated = replace(state, execution=execution)
            return memory._append_event(updated, "MILESTONE_AUTO_STOPPED", f"Zatrzymano AUTO milestone {updated_run.get('milestone_id')}: {reason}", milestone_id=updated_run.get("milestone_id"), plan_version=plan.plan_version), None

        memory.execution_transaction(transition)
        return self.milestone_auto_snapshot(project_id, run_id=run_id)

    def milestone_auto_snapshot(self, project_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        project, plan, memory = self._project(project_id)
        state = memory.read_state()
        execution = _execution_document(state)
        active = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
        selected_run = run_id or (active.get("milestone_run_id") if active else None)
        run = execution.get("milestone_runs", {}).get(selected_run) if selected_run else active
        progress = self._milestone_auto_projection(plan, state, run)
        # A GUI crash between durable v2 STOP and the v1 projection update must
        # not leave the Browser admitted by the old running milestone record.
        from .project_center_auto import CanonicalProjectCenterAutoCommands
        scope = CanonicalProjectCenterAutoCommands(self.runtime_root, project_id)
        if scope.db_path.is_file():
            scope_state = scope.snapshot(plan_available=True, plan_version=plan.plan_version)
            if scope_state.stop_fenced:
                progress = {**progress, "status": "STOPPED", "runnable_task_ids": [], "blocker": {"kind": "stop", "id": project_id, "status": "STOPPED"}}
        return {
            "schema": "bdb-milestone-auto-v1",
            "project_id": project_id,
            "plan_version": plan.plan_version,
            "milestone_run_id": selected_run,
            "milestone_id": progress.get("milestone_id"),
            "status": progress.get("status"),
            "current_task_id": progress.get("next_task_id"),
            "completed_tasks": progress.get("completed_tasks", 0),
            "total_tasks": progress.get("total_tasks", 0),
            "runnable_task_ids": list(progress.get("runnable_task_ids", [])),
            "blocker": progress.get("blocker"),
            "task_statuses": dict(execution.get("task_statuses", {})),
        }

    @staticmethod
    def _milestone_auto_projection(plan: ProjectPlan, state: ProjectMemoryState, run: Mapping[str, Any] | None) -> dict[str, Any]:
        """Project AUTO state without allowing a stopped run to look runnable.

        The durable run state is authoritative for whether Browser AUTO may
        continue.  Progress calculation remains authoritative for the
        deterministic task cursor only while the run is actually running.
        """
        milestone_id = run.get("milestone_id") if isinstance(run, Mapping) else None
        progress = milestone_auto_progress(plan, state, str(milestone_id) if milestone_id is not None else None)
        if not isinstance(run, Mapping):
            return progress
        run_status = str(run.get("status") or "")
        if run_status == "completed":
            return {**progress, "status": "MILESTONE_COMPLETED", "next_task_id": None}
        if run_status == "stopped":
            return {**progress, "status": "STOPPED", "next_task_id": run.get("current_task_id")}
        if run_status in {"blocked", "review"}:
            status = "BLOCKED" if run_status == "blocked" else "REVIEW_REQUIRED"
            current_task_id = run.get("current_task_id") or progress.get("next_task_id")
            blocker = progress.get("blocker")
            if not blocker and current_task_id:
                blocker = {"id": current_task_id, "kind": "task" if run_status == "blocked" else "review", "status": run_status}
            return {**progress, "status": status, "next_task_id": current_task_id, "blocker": blocker, "runnable_task_ids": []}
        if run_status == "running":
            return progress
        # Unknown run states are never a Browser AUTO admission.
        return {**progress, "status": "BLOCKED", "next_task_id": run.get("current_task_id") or progress.get("next_task_id"), "runnable_task_ids": []}

    @staticmethod
    def _criterion_type(criterion: str) -> str:
        lowered = criterion.strip().lower()
        if lowered.startswith(("manual:", "review:", "visual:")):
            return "MANUAL_REVIEW"
        if lowered.startswith("external:"):
            return "EXTERNAL"
        if lowered.startswith(("unknown:", "tbd:")):
            return "UNKNOWN"
        return "DETERMINISTIC"

    def _evaluate_acceptance(
        self,
        task: ProjectTask,
        *,
        project_id: str,
        plan_version: str,
        attempt_id: str,
        validation_ok: bool,
        criteria: Iterable[Mapping[str, Any]] | None,
        head_before: str | None = None,
        head_after: str | None = None,
        promotion_status: str = "NOT_RUN",
        canonical_refs: Mapping[str, Any] | None = None,
        local_repo_path: str | Path | None = None,
    ) -> TaskAcceptanceResult:
        supplied = {str(item.get("criterion")): item for item in (criteria or ()) if isinstance(item, Mapping)}
        normalized: list[Mapping[str, Any]] = []
        deterministic_failure = False; review_required = False; unknown = False
        for criterion in task.acceptance_criteria:
            item = supplied.get(criterion, {})
            raw_kind = str(item.get("type") or self._criterion_type(criterion)).upper()
            kind = "MANUAL_REVIEW" if raw_kind in {"MANUAL", "MANUAL_REVIEW"} else raw_kind
            if kind not in {"DETERMINISTIC", "MANUAL_REVIEW", "EXTERNAL", "UNKNOWN"}:
                kind = "UNKNOWN"
            status = str(item.get("status") or ("PASS" if validation_ok and kind == "DETERMINISTIC" else "REVIEW_REQUIRED" if kind in {"MANUAL_REVIEW", "EXTERNAL"} else "UNKNOWN")).upper()
            if status not in {"PASS", "FAIL", "REVIEW_REQUIRED", "UNKNOWN"}:
                status = "UNKNOWN"
            evidence_ref = item.get("evidence_ref")
            normalized.append({"criterion": criterion, "type": kind, "status": status, "evidence_ref": evidence_ref})
            if kind == "DETERMINISTIC" and status == "FAIL": deterministic_failure = True
            elif kind in {"MANUAL_REVIEW", "EXTERNAL"} and status != "PASS": review_required = True
            elif kind == "UNKNOWN" or status == "UNKNOWN": unknown = True

        if task_requires_code_delivery(task):
            verified, _reason = verify_authoritative_code_delivery(
                head_before=head_before,
                head_after=head_after,
                promotion_status=promotion_status,
                canonical_refs=canonical_refs,
                local_repo_path=local_repo_path,
                runtime_root=self.runtime_root,
                task=task,
            )
            if not verified:
                deterministic_failure = True
                normalized.append({
                    "criterion": "canonical:code_delivery_evidence",
                    "type": "DETERMINISTIC",
                    "status": "FAIL",
                    "evidence_ref": "missing_code_deliverable_evidence",
                })

        overall = "FAIL" if deterministic_failure or not validation_ok else "UNKNOWN" if unknown else "REVIEW_REQUIRED" if review_required else "PASS"
        return TaskAcceptanceResult(project_id, plan_version, task.task_id, attempt_id, tuple(normalized), overall, _utc_now())

    def record_result(self, project_id: str, result: Mapping[str, Any]) -> ProjectExecutionAttempt:
        for field in ("execution_status", "validation_status"):
            _final_result_status(result.get(field, "UNKNOWN"), field)
        _final_result_status(result.get("promotion_status", "NOT_RUN"), "promotion_status")
        project, plan, memory = self._project(project_id)
        binding_id = _identifier(result.get("execution_binding_id"), "execution_binding_id")
        binding: ProjectExecutionBinding | None = None
        stale = {"value": False, "code": None}

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, ProjectExecutionAttempt]:
            nonlocal binding
            execution = _execution_document(state)
            raw_binding = next((item for item in execution["bindings"] if item.get("execution_binding_id") == binding_id), None)
            if raw_binding is None:
                _fail("execution_binding_not_found", "execution binding does not exist")
            binding = _binding_from_dict(raw_binding)
            digest_v2 = execution_result_digest_v2(binding, result)
            digest_v1 = execution_result_digest_v1(binding, result)
            invalidated_attempt_ids = {
                item.get("attempt_id") for item in execution.get("completion_invalidations", [])
                if isinstance(item, Mapping)
            }
            existing = next(
                (
                    item for item in execution["attempts"]
                    if item.get("attempt_id") not in invalidated_attempt_ids
                    and item.get("execution_binding_id") == binding_id and (
                        item.get("result_digest") == digest_v2 or (
                            item.get("result_digest") == digest_v1 and item.get("identity_version") in (None, IDENTITY_VERSION_V1)
                        )
                    )
                ),
                None,
            )
            if existing is not None:
                replay = _attempt_from_dict(existing)
                if replay.result_status == "STALE_RESULT":
                    stale["value"] = True
                    stale["code"] = replay.failure_code or "execution_binding_stale"
                return state, replay
            result_digest = digest_v2

            stale_code: str | None = None
            current_binding_id = execution.get("current_binding_id")
            if (
                binding.project_id != project.project_id
                or binding.repo_alias != project.repo_alias
                or binding.plan_version != plan.plan_version
                or binding.superseded
                or binding.status != STATUS_ACTIVE
                or (current_binding_id is not None and current_binding_id != binding.execution_binding_id)
            ):
                stale_code = "execution_binding_stale"
            if result.get("command_id") != binding.command_id or result.get("correlation_id") != binding.correlation_id:
                stale_code = "execution_identity_mismatch"
            if result.get("project_id") not in (None, binding.project_id) or result.get("task_id") not in (None, binding.task_id) or result.get("plan_version") not in (None, binding.plan_version):
                stale_code = "execution_subject_mismatch"
            if result.get("repo_alias") not in (None, binding.repo_alias):
                stale_code = "repo_identity_mismatch"
            if result.get("head_before") not in (None, binding.expected_repo_head_before):
                stale_code = "repo_head_mismatch"
            task = next((item for item in plan.tasks if item.task_id == binding.task_id), None)
            if task is None:
                stale_code = "task_superseded"
            attempt_id = _identifier(result.get("attempt_id") or f"attempt-{uuid.uuid4().hex}", "attempt_id")
            if stale_code:
                stale["value"] = True
                stale["code"] = stale_code
                attempt = ProjectExecutionAttempt(
                    attempt_id,
                    project.project_id,
                    binding.plan_version,
                    binding.task_id,
                    binding_id,
                    binding.command_id,
                    binding.created_at,
                    _utc_now(),
                    str(result.get("head_before") or binding.expected_repo_head_before),
                    result.get("head_after"),
                    _status(result.get("execution_status", "UNKNOWN"), "execution_status"),
                    _status(result.get("validation_status", "UNKNOWN"), "validation_status"),
                    _status(result.get("promotion_status", "NOT_RUN"), "promotion_status"),
                    "STALE_RESULT",
                    _text(result.get("result_summary", "stale execution result"), "result_summary", max_length=4_000, required=False),
                    tuple(_text(item, "evidence_ref", max_length=512) for item in result.get("evidence_refs", [])),
                    stale_code,
                    result_digest,
                    identity_version=IDENTITY_VERSION_V2,
                )
                attempt_document = attempt.to_dict()
                attempt_document["canonical_refs"] = dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs", {}), Mapping) else {}
                execution["attempts"].append(attempt_document)
                execution["stale_result"] = True
                updated = replace(state, execution=execution)
                updated = memory._append_event(
                    updated,
                    "EXECUTION_STALE_RESULT",
                    f"Późny wynik zadania {binding.task_id} wymaga reconciliacji ({stale_code})",
                    task_id=binding.task_id,
                    plan_version=binding.plan_version,
                    correlation_id=binding.correlation_id,
                )
                return updated, attempt

            if task is None:
                _fail("task_not_found", "bound task does not exist")
            validation_ok = _status(result.get("validation_status", "UNKNOWN"), "validation_status") in RESULT_STATUS_SUCCESS
            head_before_val = str(result.get("head_before") or binding.expected_repo_head_before)
            head_after_val = result.get("head_after")
            promotion_status_val = _status(result.get("promotion_status", "NOT_RUN"), "promotion_status")
            canonical_refs_val = dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs", {}), Mapping) else {}

            acceptance = self._evaluate_acceptance(
                task,
                project_id=project.project_id,
                plan_version=plan.plan_version,
                attempt_id=attempt_id,
                validation_ok=validation_ok,
                criteria=result.get("criteria"),
                head_before=head_before_val,
                head_after=head_after_val,
                promotion_status=promotion_status_val,
                canonical_refs=canonical_refs_val,
                local_repo_path=project.local_repo_path,
            )
            execution_ok = _status(result.get("execution_status", "UNKNOWN"), "execution_status") in RESULT_STATUS_SUCCESS
            overall = acceptance.overall if execution_ok else "FAIL"
            failure_code = result.get("failure_code")
            if overall == "FAIL" and not failure_code:
                if any(item.get("criterion") == "canonical:code_delivery_evidence" and item.get("status") == "FAIL" for item in acceptance.criteria):
                    failure_code = "missing_code_deliverable_evidence"

            attempt = ProjectExecutionAttempt(
                attempt_id,
                project.project_id,
                binding.plan_version,
                binding.task_id,
                binding_id,
                binding.command_id,
                binding.created_at,
                _utc_now(),
                head_before_val,
                head_after_val,
                _status(result.get("execution_status", "UNKNOWN"), "execution_status"),
                _status(result.get("validation_status", "UNKNOWN"), "validation_status"),
                promotion_status_val,
                overall,
                _text(result.get("result_summary", ""), "result_summary", max_length=4_000, required=False),
                tuple(_text(item, "evidence_ref", max_length=512) for item in result.get("evidence_refs", [])),
                failure_code,
                result_digest,
                identity_version=IDENTITY_VERSION_V2,
            )
            attempt_document = attempt.to_dict()
            attempt_document["canonical_refs"] = dict(result.get("canonical_refs", {})) if isinstance(result.get("canonical_refs", {}), Mapping) else {}
            execution["attempts"].append(attempt_document)
            execution["acceptance_results"].append(acceptance.to_dict())

            # Terminalize the current binding (ACTIVE -> ACCEPTED if PASS, ACTIVE -> FAILED if not PASS)
            terminal_status = STATUS_ACCEPTED if overall == "PASS" else STATUS_FAILED
            validate_binding_transition(raw_binding.get("status", STATUS_ACTIVE), terminal_status)
            raw_binding["status"] = terminal_status
            raw_binding["finished_at"] = _utc_now()

            statuses = dict(execution.get("task_statuses", {}))
            previous = statuses.get(task.task_id, task.status)
            new_status = "completed" if overall == "PASS" else "review" if overall in {"REVIEW_REQUIRED", "UNKNOWN"} else "blocked" if result.get("failure_code") or not validation_ok else "active"
            if previous == "completed" and new_status != "completed":
                _fail("task_completed_downgrade", "completed task cannot be downgraded by an execution result")
            statuses[task.task_id] = new_status
            execution["task_statuses"] = statuses
            if new_status == "completed":
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "TASK_COMPLETED", f"Zakończono zadanie {task.task_id}; acceptance {overall}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            elif new_status == "review":
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "TASK_REVIEW", f"Zadanie {task.task_id} gotowe do przeglądu", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            elif new_status == "blocked":
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "TASK_BLOCKED", f"Zadanie {task.task_id} zablokowane: {result.get('failure_code') or 'validation_failed'}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            else:
                updated = replace(state, execution=execution)
                updated = memory._append_event(updated, "EXECUTION_COMPLETED", f"Próba zadania {task.task_id} zakończona: {overall}", task_id=task.task_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active_run and active_run.get("status") in {"running", "review", "blocked"}:
                progress = milestone_auto_progress(plan, updated, str(active_run.get("milestone_id")))
                execution = dict(updated.execution)
                execution["current_binding_id"] = None
                runs = dict(execution.get("milestone_runs", {}))
                run_id = active_run.get("milestone_run_id")
                run = dict(runs.get(run_id, active_run))
                progress_status = str(progress.get("status") or "RUNNABLE")
                if new_status == "completed":
                    run_status = "completed" if progress_status == "MILESTONE_COMPLETED" else "running"
                    next_task_id = progress.get("next_task_id")
                elif new_status in {"review", "blocked"}:
                    run_status = "review" if new_status == "review" else "blocked"
                    next_task_id = task.task_id
                else:
                    progress_status = "RUNNABLE"
                    run_status = "running"
                    next_task_id = task.task_id
                run.update({
                    "updated_at": _utc_now(),
                    "current_task_id": next_task_id,
                    "completed_tasks": progress.get("completed_tasks", 0),
                    "total_tasks": progress.get("total_tasks", 0),
                    "status": run_status,
                    "progress_status": progress_status,
                    "blocker": progress.get("blocker"),
                })
                runs[run_id] = run
                execution["milestone_runs"] = runs
                execution["active_milestone_run"] = run
                execution["current_task_id"] = next_task_id
                if progress.get("status") == "MILESTONE_COMPLETED":
                    updated = memory._append_event(updated, "MILESTONE_AUTO_COMPLETED", f"AUTO ukończył milestone {active_run.get('milestone_id')}", milestone_id=active_run.get("milestone_id"), plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            else:
                available = available_project_tasks(plan, updated)
                execution = dict(updated.execution)
                execution["current_task_id"] = available[0].task_id if len(available) == 1 else None
                execution["current_binding_id"] = None
            completed_milestones = set(execution.get("milestones_completed", []))
            for milestone in plan.milestones:
                required = [item for item in plan.tasks if item.milestone_id == milestone.milestone_id]
                if required and all(statuses.get(item.task_id, item.status) in {"completed", "skipped"} for item in required) and milestone.milestone_id not in completed_milestones:
                    completed_milestones.add(milestone.milestone_id)
                    updated = memory._append_event(updated, "MILESTONE_COMPLETED", f"Zakończono milestone {milestone.milestone_id}", milestone_id=milestone.milestone_id, plan_version=plan.plan_version, correlation_id=binding.correlation_id)
            execution["milestones_completed"] = sorted(completed_milestones)
            updated = replace(updated, execution=execution)
            return updated, attempt

        attempt = memory.execution_transaction(transition)
        if stale["value"]:
            raise ProjectExecutionError("STALE_RESULT", "execution result is stale and requires reconciliation", details={"attempt_id": attempt.attempt_id, "reason": stale["code"]})
        self.reconcile(project_id)
        return attempt

    def reconcile_project_bindings(self, project_id: str) -> None:
        _project, _plan, memory = self._project(project_id)

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state)
            reconciled = reconcile_execution_bindings(execution)
            updated = replace(state, execution=reconciled)
            updated = memory._append_event(
                updated,
                "BINDINGS_RECONCILED",
                f"Zrekoncyliowano powiązania wykonania projektu {project_id}",
            )
            return updated, None

        memory.execution_transaction(transition)

    def check_invariants(self, project_id: str) -> tuple[bool, list[str]]:
        _project, _plan, memory = self._project(project_id)
        execution = _execution_document(memory.read_state())
        return check_binding_lifecycle_invariants(execution)

    def record_bdb_finalization(self, project_id: str, binding: ProjectExecutionBinding, finalization: Any, *, criteria: Iterable[Mapping[str, Any]] | None = None, promotion_status: str = "NOT_RUN") -> ProjectExecutionAttempt:
        """Adapt an existing EngineeringLoop finalization into this binding.

        No execution is performed here; Candidate/Evidence/Publication objects
        are read and their immutable IDs are carried into the Project Memory
        attempt for exact lineage and replay checks.
        """
        validation = getattr(finalization, "validation", None)
        candidate = getattr(finalization, "candidate", None)
        candidate_view = getattr(finalization, "candidate_view", None)
        evaluation = getattr(finalization, "evaluation", None)
        publication = getattr(finalization, "publication", None)
        result = getattr(validation, "result", None)
        if validation is None or result is None or candidate is None or candidate_view is None:
            _fail("bdb_finalization_invalid", "EngineeringLoop finalization lacks canonical Candidate/validation records")
        candidate_task_id = getattr(candidate, "task_id", None)
        view_task_id = getattr(candidate_view, "task_id", None)
        if candidate_task_id not in (None, binding.task_id) or view_task_id not in (None, binding.task_id):
            _fail("bdb_finalization_binding_mismatch", "Candidate finalization is bound to a different project task")
        evidence_id = getattr(validation, "evidence_id", None)
        refs = [item for item in (evidence_id, getattr(validation, "validation_id", None), getattr(candidate, "candidate_id", None), getattr(evaluation, "evaluation_id", None), getattr(publication, "publication_id", None)) if item]
        canonical_refs = {"task_id": getattr(candidate, "task_id", None), "work_id": getattr(candidate, "work_id", None), "candidate_id": getattr(candidate, "candidate_id", None), "candidate_view_id": getattr(candidate_view, "view_id", None), "candidate_tree_digest": getattr(candidate_view, "candidate_tree_digest", None), "base_commit_oid": getattr(candidate_view, "base_commit_oid", None), "validation_id": getattr(validation, "validation_id", None), "evidence_id": evidence_id, "evaluation_id": getattr(evaluation, "evaluation_id", None), "publication_id": getattr(publication, "publication_id", None)}
        return self.record_result(project_id, {"execution_binding_id": binding.execution_binding_id, "command_id": binding.command_id, "correlation_id": binding.correlation_id, "head_before": binding.expected_repo_head_before, "head_after": getattr(candidate_view, "base_commit_oid", None) or binding.expected_repo_head_before, "execution_status": "PASS" if getattr(candidate, "state", "") in {"SEALED", "OBSERVED"} else "FAIL", "validation_status": getattr(result, "status", "UNKNOWN"), "promotion_status": promotion_status, "result_summary": "Canonical EngineeringLoop finalization", "evidence_refs": refs, "canonical_refs": canonical_refs, "criteria": list(criteria or ())})

    def approve_review(self, project_id: str, task_id: str, *, reason: str) -> None:
        project, plan, memory = self._project(project_id)
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}));
            if statuses.get(task_id) != "review": _fail("task_review_required", "task is not awaiting manual review")
            acceptance = next((item for item in reversed(execution["acceptance_results"]) if item.get("task_id") == task_id), None)
            if acceptance is None or acceptance.get("overall") != "REVIEW_REQUIRED": _fail("task_review_invalid", "task has no reviewable acceptance result")
            if any(item.get("type") == "DETERMINISTIC" and item.get("status") == "FAIL" for item in acceptance.get("criteria", [])):
                _fail("deterministic_acceptance_failed", "manual approval cannot override deterministic failure")
            statuses[task_id] = "completed"; execution["task_statuses"] = statuses
            candidate_state = replace(state, execution=execution)
            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active_run and active_run.get("status") in {"running", "review", "blocked"}:
                progress = milestone_auto_progress(plan, candidate_state, str(active_run.get("milestone_id")))
                execution["current_task_id"] = progress.get("next_task_id")
                run_id = active_run.get("milestone_run_id")
                runs = dict(execution.get("milestone_runs", {})); run = dict(runs.get(run_id, active_run))
                run.update({"current_task_id": progress.get("next_task_id"), "completed_tasks": progress.get("completed_tasks", 0), "total_tasks": progress.get("total_tasks", 0), "status": "completed" if progress.get("status") == "MILESTONE_COMPLETED" else "running", "progress_status": progress.get("status"), "updated_at": _utc_now()})
                runs[run_id] = run; execution["milestone_runs"] = runs; execution["active_milestone_run"] = run
            else:
                available = available_project_tasks(plan, candidate_state); execution["current_task_id"] = available[0].task_id if len(available) == 1 else None
            updated = replace(candidate_state, execution=execution); updated = memory._append_event(updated, "TASK_REVIEW_ACCEPTED", f"Zatwierdzono ręczny przegląd zadania {task_id}: {_text(reason, 'review_reason', max_length=2_000)}", task_id=task_id, plan_version=plan.plan_version); return updated, None
        memory.execution_transaction(transition); self.reconcile(project_id)

    def request_changes(self, project_id: str, task_id: str, *, reason: str) -> None:
        project, plan, memory = self._project(project_id)
        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}));
            if statuses.get(task_id) != "review": _fail("task_review_required", "task is not awaiting manual review")
            statuses[task_id] = "active"; execution["task_statuses"] = statuses; execution["current_task_id"] = task_id; execution["current_binding_id"] = None
            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            if active_run:
                run_id = active_run.get("milestone_run_id"); runs = dict(execution.get("milestone_runs", {})); run = dict(runs.get(run_id, active_run)); run.update({"status": "running", "current_task_id": task_id, "updated_at": _utc_now()}); runs[run_id] = run; execution["milestone_runs"] = runs; execution["active_milestone_run"] = run
            updated = replace(state, execution=execution); updated = memory._append_event(updated, "TASK_REVIEW_CHANGES_REQUESTED", f"Wymagane poprawki dla {task_id}: {_text(reason, 'review_reason', max_length=2_000)}", task_id=task_id, plan_version=plan.plan_version); return updated, None
        memory.execution_transaction(transition); self.reconcile(project_id)

    def request_project_review(self, project_id: str, *, reason: str = "review requested") -> None:
        project, plan, memory = self._project(project_id)
        memory.append_event("PROJECT_REVIEW_REQUESTED", _text(reason, "review_reason", max_length=2_000), plan_version=plan.plan_version)

    def reconcile(self, project_id: str) -> ProjectRecord:
        project, plan, memory = self._project(project_id)
        state = memory.read_state(); execution = _execution_document(state); statuses = dict(execution.get("task_statuses", {}))
        completed = sum(statuses.get(task.task_id, task.status) in {"completed", "skipped"} for task in plan.tasks)
        active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
        run_status = str(active_run.get("status") or "") if active_run else None
        auto_progress = milestone_auto_progress(plan, state, str(active_run.get("milestone_id"))) if run_status == "running" else None
        available = available_project_tasks(plan, state, str(active_run.get("milestone_id"))) if run_status == "running" and active_run else available_project_tasks(plan, state)
        if "current_task_id" in execution:
            current_id = execution.get("current_task_id")
            if run_status == "running":
                current_id = auto_progress.get("next_task_id") if auto_progress else current_id
            elif run_status in {"review", "blocked"}:
                current_id = active_run.get("current_task_id") or current_id
            elif current_id is None and not (active_run and active_run.get("status") in {"completed", "stopped"}) and len(available) == 1:
                current_id = available[0].task_id
        else:
            current_id = plan.current_task_id if completed < len(plan.tasks) else None
        current = next((item for item in plan.tasks if item.task_id == current_id), None)
        has_blocked = any(statuses.get(task.task_id, task.status) == "blocked" for task in plan.tasks)
        has_review = any(statuses.get(task.task_id, task.status) == "review" for task in plan.tasks)
        project_status = "completed" if completed == len(plan.tasks) and plan.tasks else "blocked" if has_blocked else "active"
        if has_review and project_status == "active":
            project_status = "active"
        if execution.get("current_task_id") != current_id:
            def set_pointer(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
                current_execution = _execution_document(state); current_execution["current_task_id"] = current_id
                return replace(state, execution=current_execution), None
            memory.execution_transaction(set_pointer)
        updated = ProjectRecord(**{**project.__dict__, "project_status": project_status, "plan_imported": True, "plan_version": plan.plan_version, "total_tasks": len(plan.tasks), "completed_tasks": completed, "current_milestone": current.milestone_id if current else None, "current_task": current_id})
        return self.catalog.upsert(updated)

    def invalidate_task_completion(
        self,
        project_id: str,
        task_id: str,
        *,
        attempt_id: str | None = None,
        expected_result_digest: str | None = None,
        reason: str = "invalidation_requested",
        correlation_id: str | None = None,
        invalidated_by: str = "operator",
    ) -> dict[str, Any]:
        """Atomically and auditably invalidate a task completion and reconcile state.

        - Preserves the historical attempt unmodified in Project Memory.
        - Adds an append-only CompletionInvalidation record.
        - Reverts task status from completed to active.
        - Reverts downstream tasks and supersedes their active execution bindings.
        - Reconciles current_task_id to task_id and clears current_binding_id.
        - Deterministically recalculates milestone progress via milestone_auto_progress.
        - Appends TASK_COMPLETION_INVALIDATED event to Project Memory.
        - Reconciles project record in the catalog.
        - Is strictly idempotent upon identical task/attempt replay.
        """
        task_identifier = _identifier(task_id, "task_id")
        project, plan, memory = self._project(project_id)
        target_task = next((t for t in plan.tasks if t.task_id == task_identifier), None)
        if target_task is None:
            _fail("task_not_found", f"task '{task_id}' does not exist in canonical plan")

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, dict[str, Any]]:
            execution = _execution_document(state)
            statuses = dict(execution.get("task_statuses", {}))
            invalidations = list(execution.get("completion_invalidations", []))

            # 1. Idempotency check
            matching_invalidation = next(
                (
                    item for item in invalidations
                    if item.get("task_id") == task_identifier
                    and (attempt_id is None or item.get("attempt_id") == attempt_id)
                ),
                None,
            )
            if matching_invalidation is not None:
                if expected_result_digest and matching_invalidation.get("result_digest") != expected_result_digest:
                    _fail(
                        "invalidation_conflict",
                        f"existing invalidation digest '{matching_invalidation.get('result_digest')}' differs from expected '{expected_result_digest}'",
                    )
                if statuses.get(task_identifier) in {"active", "pending"}:
                    receipt = {
                        "schema": COMPLETION_INVALIDATION_SCHEMA,
                        "status": "ALREADY_INVALIDATED",
                        "project_id": project_id,
                        "task_id": task_identifier,
                        "invalidated_task_id": task_identifier,
                        "attempt_id": matching_invalidation["attempt_id"],
                        "invalidated_attempt_id": matching_invalidation["attempt_id"],
                        "invalidation_id": matching_invalidation["invalidation_id"],
                        "superseded_downstream_bindings": [],
                        "current_task_id": execution.get("current_task_id"),
                        "idempotent": True,
                    }
                    return state, receipt

            # 2. Verify task is completed
            current_status = statuses.get(task_identifier, target_task.status)
            if current_status != "completed":
                _fail("task_not_completed", f"task '{task_identifier}' has status '{current_status}', expected 'completed'")

            # 3. Locate the target PASS attempt
            attempts = list(execution.get("attempts", []))
            if attempt_id:
                target_attempt = next((a for a in attempts if a.get("attempt_id") == attempt_id), None)
                if target_attempt is None:
                    _fail("attempt_id_mismatch", f"attempt '{attempt_id}' does not exist")
                if target_attempt.get("task_id") != task_identifier:
                    _fail("attempt_task_mismatch", f"attempt '{attempt_id}' belongs to task '{target_attempt.get('task_id')}', not '{task_identifier}'")
            else:
                target_attempt = next(
                    (a for a in reversed(attempts) if a.get("task_id") == task_identifier and a.get("result_status") == "PASS"),
                    None,
                )
                if target_attempt is None:
                    _fail("attempt_not_found", f"no accepted PASS attempt found for task '{task_identifier}'")

            if target_attempt.get("result_status") != "PASS":
                _fail("attempt_not_accepted", f"attempt '{target_attempt.get('attempt_id')}' has status '{target_attempt.get('result_status')}', not 'PASS'")

            actual_digest = target_attempt.get("result_digest", "")
            if expected_result_digest and actual_digest != expected_result_digest:
                _fail(
                    "result_digest_mismatch",
                    f"attempt result digest '{actual_digest}' does not match expected '{expected_result_digest}'",
                )

            # 4. Append invalidation record
            invalidation_id = f"invalidation-{uuid.uuid4().hex}"
            now_iso = _utc_now()
            corr_id = correlation_id or target_attempt.get("correlation_id") or f"corr-{uuid.uuid4().hex}"
            invalidation_record = {
                "invalidation_id": invalidation_id,
                "project_id": project_id,
                "plan_version": str(plan.plan_version),
                "task_id": task_identifier,
                "attempt_id": target_attempt["attempt_id"],
                "execution_binding_id": target_attempt["execution_binding_id"],
                "result_digest": actual_digest,
                "reason": _text(reason, "invalidation_reason", max_length=2_000),
                "invalidated_by": _text(invalidated_by, "invalidated_by", max_length=128),
                "created_at": now_iso,
                "correlation_id": corr_id,
            }
            invalidations.append(invalidation_record)
            execution["completion_invalidations"] = invalidations

            # 5. Revert task status to active
            statuses[task_identifier] = "active"

            # 6. Downstream handling
            downstream_task_ids = set(transitive_dependents(plan.tasks, task_identifier))

            for down_id in downstream_task_ids:
                if statuses.get(down_id) == "active":
                    statuses[down_id] = "pending"

            execution["task_statuses"] = statuses

            superseded_binding_ids: list[str] = []
            bindings = list(execution.get("bindings", []))
            for b in bindings:
                if b.get("task_id") in downstream_task_ids:
                    if b.get("status") == STATUS_ACTIVE and not b.get("superseded"):
                        validate_binding_transition(STATUS_ACTIVE, STATUS_SUPERSEDED)
                        b["status"] = STATUS_SUPERSEDED
                        b["superseded"] = True
                        if not b.get("finished_at"):
                            b["finished_at"] = now_iso
                        b["superseded_reason"] = f"downstream_of_invalidated_task_{task_identifier}"
                        superseded_binding_ids.append(b.get("execution_binding_id"))
            execution["bindings"] = bindings

            # 7. Reconcile pointers
            execution["current_task_id"] = task_identifier
            execution["current_binding_id"] = None

            milestones_completed = list(execution.get("milestones_completed", []))
            if target_task.milestone_id in milestones_completed:
                milestones_completed.remove(target_task.milestone_id)
                execution["milestones_completed"] = milestones_completed

            # 8. Reconcile active milestone run
            candidate_state = replace(state, execution=execution)
            active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
            milestone_id = active_run.get("milestone_id") if active_run else target_task.milestone_id
            progress = milestone_auto_progress(plan, candidate_state, str(milestone_id) if milestone_id else None)

            if active_run:
                runs = dict(execution.get("milestone_runs", {}))
                run_id = active_run.get("milestone_run_id")
                run = dict(runs.get(run_id, active_run))
                run.update({
                    "current_task_id": task_identifier,
                    "completed_tasks": progress.get("completed_tasks", 0),
                    "total_tasks": progress.get("total_tasks", 0),
                    "status": "running",
                    "progress_status": progress.get("status", "RUNNABLE"),
                    "updated_at": now_iso,
                })
                runs[run_id] = run
                execution["milestone_runs"] = runs
                execution["active_milestone_run"] = run

            updated = replace(state, execution=execution)
            updated = memory._append_event(
                updated,
                "TASK_COMPLETION_INVALIDATED",
                f"Unieważniono ukończenie zadania {task_identifier} (attempt {target_attempt['attempt_id'][:16]}): {reason}",
                task_id=task_identifier,
                plan_version=plan.plan_version,
                correlation_id=corr_id,
            )
            receipt = {
                "schema": COMPLETION_INVALIDATION_SCHEMA,
                "status": "INVALIDATED",
                "project_id": project_id,
                "task_id": task_identifier,
                "invalidated_task_id": task_identifier,
                "attempt_id": target_attempt["attempt_id"],
                "invalidated_attempt_id": target_attempt["attempt_id"],
                "invalidation_id": invalidation_id,
                "invalidated_by": invalidated_by,
                "superseded_downstream_bindings": superseded_binding_ids,
                "current_task_id": task_identifier,
                "milestone_completed_tasks": progress.get("completed_tasks", 0),
                "milestone_total_tasks": progress.get("total_tasks", 0),
                "milestone_auto_progress": {
                    "completed_tasks": progress.get("completed_tasks", 0),
                    "total_tasks": progress.get("total_tasks", 0),
                },
                "idempotent": False,
            }
            return updated, receipt

        receipt = memory.execution_transaction(transition)
        self.reconcile(project_id)
        return receipt

    def snapshot(self, project_id: str) -> dict[str, Any]:
        project, plan, memory = self._project(project_id); state = memory.read_state(); execution = _execution_document(state)
        active_run = execution.get("active_milestone_run") if isinstance(execution.get("active_milestone_run"), Mapping) else None
        auto_progress = self._milestone_auto_projection(plan, state, active_run) if active_run else None
        return {"schema": PROJECT_EXECUTION_SCHEMA, "project_id": project_id, "plan_version": plan.plan_version, "task_statuses": dict(execution.get("task_statuses", {})), "gate_statuses": dict(execution.get("gate_statuses", {})), "open_question_statuses": dict(execution.get("open_question_statuses", {})), "bindings": list(execution["bindings"]), "attempts": list(execution["attempts"]), "acceptance_results": list(execution["acceptance_results"]), "checkpoints": dict(execution.get("checkpoints", {})), "launch_handoffs": dict(execution.get("launch_handoffs", {})), "launch_outbox": dict(execution.get("launch_outbox", {})), "completion_invalidations": list(execution.get("completion_invalidations", [])), "current_binding_id": execution.get("current_binding_id"), "current_task_id": execution.get("current_task_id"), "available_tasks": [task.task_id for task in (available_project_tasks(plan, state, str(active_run.get("milestone_id"))) if active_run else available_project_tasks(plan, state))], "milestone_auto": {**(auto_progress or {}), "milestone_run_id": active_run.get("milestone_run_id")} if active_run and auto_progress else None, "watchdog": self.watchdog(project_id), "stale_result": bool(execution.get("stale_result", False))}


__all__ = [
    "COMPLETION_INVALIDATION_SCHEMA",
    "PROJECT_EXECUTION_SCHEMA",
    "PROJECT_EXECUTION_SUBMISSION_SCHEMA",
    "PROJECT_EXECUTION_CHECKPOINT_SCHEMA",
    "PROJECT_LAUNCH_HANDOFF_SCHEMA",
    "PROJECT_LAUNCH_OUTBOX_SCHEMA",
    "OUTBOX_STATUS_PENDING",
    "OUTBOX_STATUS_PUBLISHED",
    "OUTBOX_STATUS_ACKNOWLEDGED",
    "OUTBOX_STATUS_VALUES",
    "CompletionInvalidation",
    "ProjectExecutionAttempt",
    "ProjectExecutionBinding",
    "ProjectExecutionSubmission",
    "ProjectExecutionCoordinator",
    "ProjectExecutionError",
    "ProjectLaunchOutboxRecord",
    "TaskAcceptanceResult",
    "execution_result_digest",
    "has_canonical_code_evidence",
    "task_requires_code_delivery",
    "transitive_dependents",
    "verify_authoritative_code_delivery",
]
