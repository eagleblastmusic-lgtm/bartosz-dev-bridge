from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from bdb_operator import OperatorApi, OperatorResponse


GUI_PREPARE_PLAN_SCHEMA = "bdb-gui-prepare-plan-v1"
GUI_PREPARE_RESULT_SCHEMA = "bdb-gui-prepare-result-v1"
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_ALLOWED_PATHS = 100


class PrepareOperator(Protocol):
    def prepare(
        self,
        workspace_root: str | Path,
        *,
        source_repo: str | Path,
        alias: str,
        allowed_paths: Sequence[str],
        native_config: str | Path | None = None,
        test_timeout_seconds: int = 120,
        max_patch_bytes: int = 262_144,
        max_changed_files: int = 20,
        auto_send_max_bytes: int = 24_000,
        worker_timeout_seconds: int = 240,
        python_executable: str | Path | None = None,
    ) -> OperatorResponse:
        ...


@dataclass(frozen=True)
class PreparePlan:
    alias: str
    workspace_root: str
    source_repo: str
    allowed_paths: tuple[str, ...]
    python_executable: str
    test_timeout_seconds: int
    max_patch_bytes: int
    max_changed_files: int
    auto_send_max_bytes: int
    worker_timeout_seconds: int
    native_config: str | None = None
    requires_confirmation: bool = True
    preflight_owner: str = "existing_prepare_workspace_loop"
    read_only: bool = True
    mutation_operations_invoked: int = 0
    schema: str = GUI_PREPARE_PLAN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "alias": self.alias,
            "workspace_root": self.workspace_root,
            "source_repo": self.source_repo,
            "allowed_paths": list(self.allowed_paths),
            "python_executable": self.python_executable,
            "test_timeout_seconds": self.test_timeout_seconds,
            "max_patch_bytes": self.max_patch_bytes,
            "max_changed_files": self.max_changed_files,
            "auto_send_max_bytes": self.auto_send_max_bytes,
            "worker_timeout_seconds": self.worker_timeout_seconds,
            "native_config": self.native_config,
            "requires_confirmation": self.requires_confirmation,
            "preflight_owner": self.preflight_owner,
            "read_only": self.read_only,
            "mutation_operations_invoked": self.mutation_operations_invoked,
        }


@dataclass(frozen=True)
class PrepareResult:
    plan: PreparePlan
    ok: bool
    operation_id: str
    project_alias: str | None
    operator_data: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None
    mutation_operations_invoked: int = 1
    schema: str = GUI_PREPARE_RESULT_SCHEMA

    @classmethod
    def from_response(cls, plan: PreparePlan, response: OperatorResponse) -> "PrepareResult":
        return cls(
            plan=plan,
            ok=response.ok,
            operation_id=response.operation_id,
            project_alias=response.project_alias,
            operator_data=dict(response.data) if response.ok else {},
            error_code=response.error.code if response.error is not None else None,
            error_message=response.error.message if response.error is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plan": self.plan.to_dict(),
            "ok": self.ok,
            "operation_id": self.operation_id,
            "project_alias": self.project_alias,
            "operator_data": dict(self.operator_data),
            "mutation_operations_invoked": self.mutation_operations_invoked,
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }


class ProjectPrepareService:
    """Builds a read-only plan and invokes the existing preparer after confirmation."""

    def __init__(self, operator: PrepareOperator | None = None) -> None:
        self._operator = operator or OperatorApi()

    def build_plan(
        self,
        *,
        workspaces_root: str | Path,
        alias: str,
        source_repo: str | Path,
        allowed_paths: Sequence[str],
        python_executable: str | Path | None = None,
        native_config: str | Path | None = None,
        test_timeout_seconds: int = 120,
        max_patch_bytes: int = 262_144,
        max_changed_files: int = 20,
        auto_send_max_bytes: int = 24_000,
        worker_timeout_seconds: int = 240,
    ) -> PreparePlan:
        normalized_alias = alias.strip()
        if not _ALIAS.fullmatch(normalized_alias):
            raise ValueError("alias must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")

        workspace_parent = Path(workspaces_root).expanduser().resolve(strict=False)
        if not workspace_parent.is_dir():
            raise ValueError("workspaces_root must be an existing directory")
        workspace_root = (workspace_parent / normalized_alias).resolve(strict=False)
        try:
            workspace_root.relative_to(workspace_parent)
        except ValueError as error:
            raise ValueError("workspace_root must stay inside workspaces_root") from error
        if workspace_root.exists():
            raise ValueError("workspace_root already exists")

        source = Path(source_repo).expanduser().resolve(strict=False)
        if not source.is_dir() or not source.joinpath(".git").exists():
            raise ValueError("source_repo must be an existing non-bare Git checkout")
        if workspace_parent == source or workspace_parent in source.parents:
            raise ValueError("workspaces_root must not be inside source_repo")

        paths = _normalize_allowed_paths(allowed_paths)
        python = Path(python_executable or sys.executable).expanduser().resolve(strict=False)
        if not python.is_file():
            raise ValueError("python_executable must be an existing file")
        native = (
            str(Path(native_config).expanduser().resolve(strict=False))
            if native_config is not None and str(native_config).strip()
            else None
        )
        _bounded_int("test_timeout_seconds", test_timeout_seconds, 1, 3_600)
        _bounded_int("max_patch_bytes", max_patch_bytes, 1, 16_777_216)
        _bounded_int("max_changed_files", max_changed_files, 1, 1_000)
        _bounded_int("auto_send_max_bytes", auto_send_max_bytes, 1, 1_048_576)
        _bounded_int("worker_timeout_seconds", worker_timeout_seconds, 1, 3_600)

        return PreparePlan(
            alias=normalized_alias,
            workspace_root=str(workspace_root),
            source_repo=str(source),
            allowed_paths=paths,
            python_executable=str(python),
            native_config=native,
            test_timeout_seconds=test_timeout_seconds,
            max_patch_bytes=max_patch_bytes,
            max_changed_files=max_changed_files,
            auto_send_max_bytes=auto_send_max_bytes,
            worker_timeout_seconds=worker_timeout_seconds,
        )

    def execute(self, plan: PreparePlan) -> PrepareResult:
        if not isinstance(plan, PreparePlan):
            raise TypeError("prepare requires a validated PreparePlan")
        response = self._operator.prepare(
            plan.workspace_root,
            source_repo=plan.source_repo,
            alias=plan.alias,
            allowed_paths=plan.allowed_paths,
            native_config=plan.native_config,
            test_timeout_seconds=plan.test_timeout_seconds,
            max_patch_bytes=plan.max_patch_bytes,
            max_changed_files=plan.max_changed_files,
            auto_send_max_bytes=plan.auto_send_max_bytes,
            worker_timeout_seconds=plan.worker_timeout_seconds,
            python_executable=plan.python_executable,
        )
        return PrepareResult.from_response(plan, response)


def _normalize_allowed_paths(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("allowed_paths must be a sequence of path patterns")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("allowed_paths entries must be strings")
        value = raw.strip().replace("\\", "/")
        if not value:
            continue
        if value.startswith(("/", "../")) or "/../" in f"/{value}/" or ":" in value:
            raise ValueError(f"allowed path is absolute or escapes the repository: {raw}")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    if not normalized:
        raise ValueError("at least one allowed path is required")
    if len(normalized) > MAX_ALLOWED_PATHS:
        raise ValueError(f"allowed_paths cannot contain more than {MAX_ALLOWED_PATHS} entries")
    return tuple(normalized)


def _bounded_int(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
