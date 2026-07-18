from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .errors import OperatorApiError, OperatorErrorCode
from .models import OPERATOR_PROJECT_SCHEMA, OperatorError, OperatorResponse
from .runner import CommandRunner, CompletedCommand, SubprocessCommandRunner


WORKSPACE_STATE_SCHEMA = "bdb-workspace-loop-state-v1"
_SAFE_ALIAS = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class WorkspaceState:
    root: Path
    alias: str
    source_repo: Path
    source_branch: str
    python_executable: Path
    native_config: Path
    allowed_paths: tuple[str, ...]
    configured_status: str


class OperatorApi:
    """Local, in-process application facade over the existing BDB operator.

    The class exposes a closed operation catalog and always returns a versioned
    ``OperatorResponse``. It does not read or mutate the Bridge Journal directly.
    """

    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        runner: CommandRunner | None = None,
        powershell_executable: str | None = None,
        platform_name: str | None = None,
        default_timeout_seconds: float = 60.0,
    ) -> None:
        self._repo_root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
        self._runner = runner or SubprocessCommandRunner()
        self._platform_name = platform_name or os.name
        self._powershell = powershell_executable or "powershell.exe"
        if not 1.0 <= default_timeout_seconds <= 3_600.0:
            raise ValueError("default_timeout_seconds must be between 1 and 3600")
        self._default_timeout_seconds = float(default_timeout_seconds)

    def capabilities(self) -> OperatorResponse:
        return OperatorResponse.success(
            "capabilities",
            data={
                "read_operations": ["capabilities", "list_projects", "status"],
                "mutation_operations": ["prepare", "start", "stop", "rearm"],
                "transport": "in_process",
                "network_listener": False,
                "arbitrary_shell": False,
            },
        )

    def list_projects(self, workspaces_root: str | Path) -> OperatorResponse:
        operation_id = str(uuid4())
        try:
            root = Path(workspaces_root).expanduser().resolve(strict=False)
            if not root.is_dir():
                raise OperatorApiError(
                    OperatorErrorCode.INVALID_ARGUMENT,
                    "Workspaces root does not exist or is not a directory",
                    details={"workspaces_root": str(root)},
                )
            projects: list[dict[str, Any]] = []
            invalid_entries: list[dict[str, str]] = []
            for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
                if not child.is_dir():
                    continue
                state_path = child / "workspace-loop-state.json"
                if not state_path.is_file():
                    continue
                try:
                    state = self._load_state(child)
                except OperatorApiError as error:
                    invalid_entries.append({"path": str(child), "code": error.code.value})
                    continue
                projects.append(self._project_document(state))
            return OperatorResponse.success(
                "list_projects",
                operation_id=operation_id,
                data={
                    "workspaces_root": str(root),
                    "projects": projects,
                    "invalid_entries": invalid_entries,
                },
            )
        except OperatorApiError as error:
            return self._failure("list_projects", operation_id, error)
        except Exception as error:  # defensive API boundary
            return self._unexpected_failure("list_projects", operation_id, error)

    def status(self, workspace_root: str | Path) -> OperatorResponse:
        return self._workspace_action("status", workspace_root, "Status")

    def start(self, workspace_root: str | Path, *, arm_minutes: int = 30) -> OperatorResponse:
        if not 1 <= arm_minutes <= 60:
            return self._argument_failure(
                "start",
                "arm_minutes must be between 1 and 60",
                details={"arm_minutes": arm_minutes},
            )
        return self._workspace_action(
            "start",
            workspace_root,
            "Start",
            extra_args=("-ArmMinutes", str(arm_minutes)),
        )

    def stop(self, workspace_root: str | Path) -> OperatorResponse:
        return self._workspace_action("stop", workspace_root, "Stop")

    def rearm(self, workspace_root: str | Path, *, arm_minutes: int = 30) -> OperatorResponse:
        operation_id = str(uuid4())
        alias: str | None = None
        if not 1 <= arm_minutes <= 60:
            return self._argument_failure(
                "rearm",
                "arm_minutes must be between 1 and 60",
                details={"arm_minutes": arm_minutes},
                operation_id=operation_id,
            )
        try:
            self._require_windows()
            state = self._load_state(workspace_root)
            alias = state.alias
            completed = self._run(
                (
                    str(state.python_executable),
                    "-m",
                    "bdb_bridge",
                    "bridge",
                    "native-host",
                    "arm",
                    "--config",
                    str(state.native_config),
                    "--minutes",
                    str(arm_minutes),
                )
            )
            data = self._decode_json(completed, operation="rearm")
            return OperatorResponse.success(
                "rearm",
                operation_id=operation_id,
                project_alias=alias,
                data=data,
            )
        except OperatorApiError as error:
            return self._failure("rearm", operation_id, error, project_alias=alias)
        except Exception as error:
            return self._unexpected_failure("rearm", operation_id, error, project_alias=alias)

    def prepare(
        self,
        workspace_root: str | Path,
        *,
        source_repo: str | Path,
        alias: str,
        allowed_paths: Iterable[str],
        test_timeout_seconds: float = 120.0,
        python_executable: str | Path | None = None,
    ) -> OperatorResponse:
        operation_id = str(uuid4())
        try:
            self._require_windows()
            if not _SAFE_ALIAS.fullmatch(alias):
                raise OperatorApiError(
                    OperatorErrorCode.INVALID_ARGUMENT,
                    "Alias must match ^[a-z][a-z0-9-]{0,31}$",
                    details={"alias": alias},
                )
            normalized_paths = tuple(str(path).strip() for path in allowed_paths if str(path).strip())
            if not normalized_paths:
                raise OperatorApiError(
                    OperatorErrorCode.INVALID_ARGUMENT,
                    "At least one allowed path is required",
                )
            if not 1.0 <= float(test_timeout_seconds) <= 3_600.0:
                raise OperatorApiError(
                    OperatorErrorCode.INVALID_ARGUMENT,
                    "test_timeout_seconds must be between 1 and 3600",
                    details={"test_timeout_seconds": test_timeout_seconds},
                )
            preparer = self._repo_root / "scripts" / "prepare_workspace_loop.py"
            if not preparer.is_file():
                raise OperatorApiError(
                    OperatorErrorCode.OPERATOR_SCRIPT_MISSING,
                    "Workspace preparer is missing",
                    details={"path": str(preparer)},
                )
            executable = Path(python_executable or sys.executable).expanduser().resolve(strict=False)
            args: list[str] = [
                str(executable),
                str(preparer),
                "--root",
                str(Path(workspace_root).expanduser().resolve(strict=False)),
                "--repo",
                str(Path(source_repo).expanduser().resolve(strict=False)),
                "--alias",
                alias,
                "--python",
                str(executable),
                "--test-timeout",
                str(float(test_timeout_seconds)),
            ]
            for path in normalized_paths:
                args.extend(("--allowed-path", path))
            completed = self._run(tuple(args), timeout_seconds=max(60.0, float(test_timeout_seconds) + 30.0))
            data = self._decode_json(completed, operation="prepare")
            return OperatorResponse.success(
                "prepare",
                operation_id=operation_id,
                project_alias=alias,
                data=data,
            )
        except OperatorApiError as error:
            return self._failure("prepare", operation_id, error, project_alias=alias or None)
        except Exception as error:
            return self._unexpected_failure("prepare", operation_id, error, project_alias=alias or None)

    def _workspace_action(
        self,
        operation: str,
        workspace_root: str | Path,
        action: str,
        *,
        extra_args: tuple[str, ...] = (),
    ) -> OperatorResponse:
        operation_id = str(uuid4())
        alias: str | None = None
        try:
            self._require_windows()
            state = self._load_state(workspace_root)
            alias = state.alias
            script = self._repo_root / "scripts" / "Invoke-BDBWorkspaceLoop.ps1"
            if not script.is_file():
                raise OperatorApiError(
                    OperatorErrorCode.OPERATOR_SCRIPT_MISSING,
                    "Workspace operator script is missing",
                    details={"path": str(script)},
                )
            args = (
                self._powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Action",
                action,
                "-Root",
                str(state.root),
                *extra_args,
            )
            completed = self._run(args)
            data = self._decode_json(completed, operation=operation)
            return OperatorResponse.success(
                operation,
                operation_id=operation_id,
                project_alias=alias,
                data=data,
            )
        except OperatorApiError as error:
            return self._failure(operation, operation_id, error, project_alias=alias)
        except Exception as error:
            return self._unexpected_failure(operation, operation_id, error, project_alias=alias)

    def _load_state(self, workspace_root: str | Path) -> WorkspaceState:
        root = Path(workspace_root).expanduser().resolve(strict=False)
        state_path = root / "workspace-loop-state.json"
        if not state_path.is_file():
            raise OperatorApiError(
                OperatorErrorCode.WORKSPACE_STATE_MISSING,
                "Workspace loop state is missing",
                details={"path": str(state_path)},
            )
        try:
            document = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise OperatorApiError(
                OperatorErrorCode.WORKSPACE_STATE_INVALID,
                "Workspace loop state is not valid JSON",
                details={"path": str(state_path), "reason": str(error)},
            ) from error
        if not isinstance(document, dict) or document.get("schema") != WORKSPACE_STATE_SCHEMA:
            raise OperatorApiError(
                OperatorErrorCode.WORKSPACE_STATE_INVALID,
                "Workspace loop state schema is unsupported",
                details={"path": str(state_path), "schema": document.get("schema") if isinstance(document, dict) else None},
            )
        required = (
            "alias",
            "source_repo",
            "source_branch",
            "python_executable",
            "native_config",
        )
        for key in required:
            if not isinstance(document.get(key), str) or not document[key].strip():
                raise OperatorApiError(
                    OperatorErrorCode.WORKSPACE_STATE_INVALID,
                    f"Workspace loop state field is missing or invalid: {key}",
                    details={"path": str(state_path), "field": key},
                )
        allowed = document.get("allowed_paths")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise OperatorApiError(
                OperatorErrorCode.WORKSPACE_STATE_INVALID,
                "Workspace loop allowed_paths is invalid",
                details={"path": str(state_path)},
            )
        return WorkspaceState(
            root=root,
            alias=document["alias"],
            source_repo=Path(document["source_repo"]),
            source_branch=document["source_branch"],
            python_executable=Path(document["python_executable"]),
            native_config=Path(document["native_config"]),
            allowed_paths=tuple(allowed),
            configured_status=str(document.get("status", "unknown")),
        )

    def _project_document(self, state: WorkspaceState) -> dict[str, Any]:
        return {
            "schema": OPERATOR_PROJECT_SCHEMA,
            "alias": state.alias,
            "workspace_root": str(state.root),
            "source_repo": str(state.source_repo),
            "source_branch": state.source_branch,
            "configured_status": state.configured_status,
            "allowed_paths": list(state.allowed_paths),
        }

    def _require_windows(self) -> None:
        if self._platform_name != "nt":
            raise OperatorApiError(
                OperatorErrorCode.UNSUPPORTED_PLATFORM,
                "BDB workspace operator currently supports Windows only",
                details={"platform": self._platform_name},
            )

    def _run(
        self,
        args: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> CompletedCommand:
        try:
            completed = self._runner.run(
                args,
                timeout_seconds=timeout_seconds or self._default_timeout_seconds,
            )
        except FileNotFoundError as error:
            raise OperatorApiError(
                OperatorErrorCode.EXECUTABLE_MISSING,
                "Operator executable was not found",
                details={"executable": args[0]},
            ) from error
        except subprocess.TimeoutExpired as error:
            raise OperatorApiError(
                OperatorErrorCode.COMMAND_TIMEOUT,
                "Operator command timed out",
                details={"timeout_seconds": error.timeout},
            ) from error
        if completed.returncode != 0:
            raise OperatorApiError(
                OperatorErrorCode.COMMAND_FAILED,
                "Operator command failed",
                details={
                    "returncode": completed.returncode,
                    "stderr_tail": completed.stderr[-4_000:],
                    "stdout_tail": completed.stdout[-4_000:],
                },
            )
        return completed

    def _decode_json(self, completed: CompletedCommand, *, operation: str) -> dict[str, Any]:
        text = completed.stdout.strip()
        if not text:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_RESPONSE,
                "Operator command returned no JSON",
                details={"operation": operation},
            )
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_RESPONSE,
                "Operator command returned invalid JSON",
                details={"operation": operation, "reason": str(error), "stdout_tail": text[-4_000:]},
            ) from error
        if not isinstance(document, dict):
            raise OperatorApiError(
                OperatorErrorCode.INVALID_RESPONSE,
                "Operator command JSON must be an object",
                details={"operation": operation},
            )
        return document

    def _failure(
        self,
        operation: str,
        operation_id: str,
        error: OperatorApiError,
        *,
        project_alias: str | None = None,
    ) -> OperatorResponse:
        return OperatorResponse.failure(
            operation,
            operation_id=operation_id,
            project_alias=project_alias,
            error=OperatorError(
                code=error.code.value,
                message=str(error),
                details=error.details,
            ),
        )

    def _unexpected_failure(
        self,
        operation: str,
        operation_id: str,
        error: Exception,
        *,
        project_alias: str | None = None,
    ) -> OperatorResponse:
        wrapped = OperatorApiError(
            OperatorErrorCode.INTERNAL_ERROR,
            "Unexpected Operator API failure",
            details={"type": type(error).__name__, "message": str(error)},
        )
        return self._failure(operation, operation_id, wrapped, project_alias=project_alias)

    def _argument_failure(
        self,
        operation: str,
        message: str,
        *,
        details: dict[str, Any],
        operation_id: str | None = None,
    ) -> OperatorResponse:
        return self._failure(
            operation,
            operation_id or str(uuid4()),
            OperatorApiError(OperatorErrorCode.INVALID_ARGUMENT, message, details=details),
        )
