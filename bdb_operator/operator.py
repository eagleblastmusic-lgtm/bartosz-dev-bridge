from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .api import OperatorApi as ExecutionOperatorApi
from .errors import OperatorApiError, OperatorErrorCode
from .models import OperatorResponse
from .observability import ObservabilityReader
from .runner import CommandRunner


POWERSHELL_REQUIRED_MAJOR = 7
POWERSHELL_VERSION_COMMAND = "$PSVersionTable.PSVersion.Major"


class OperatorApi(ExecutionOperatorApi):
    """Public Operator API including P03 execution and P04 read projections."""

    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        runner: CommandRunner | None = None,
        powershell_executable: str | None = None,
        platform_name: str | None = None,
        default_timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(
            repo_root=repo_root,
            runner=runner,
            powershell_executable=powershell_executable or "pwsh.exe",
            platform_name=platform_name,
            default_timeout_seconds=default_timeout_seconds,
        )
        self._powershell_major: int | None = None

    def capabilities(self) -> OperatorResponse:
        response = super().capabilities()
        data = dict(response.data)
        data["read_operations"] = [
            "capabilities",
            "list_projects",
            "status",
            "events",
            "current_operation",
            "logs",
        ]
        data["event_schema"] = "bdb-event-v1"
        data["journal_access"] = "read_only"
        data["powershell"] = {
            "executable": self._powershell,
            "required_major": POWERSHELL_REQUIRED_MAJOR,
            "validated_major": self._powershell_major,
            "fallback_to_windows_powershell": False,
        }
        return OperatorResponse.success("capabilities", data=data)

    def events(
        self,
        workspace_root: str | Path,
        *,
        after_event_id: int = 0,
        limit: int = 100,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> OperatorResponse:
        return self._observability_action(
            "events",
            workspace_root,
            lambda reader: reader.list_events(
                after_event_id=after_event_id,
                limit=limit,
                session_id=session_id,
                command_id=command_id,
            ),
        )

    def current_operation(self, workspace_root: str | Path) -> OperatorResponse:
        return self._observability_action(
            "current_operation",
            workspace_root,
            lambda reader: reader.current_operation(),
        )

    def logs(
        self,
        workspace_root: str | Path,
        *,
        max_bytes: int = 65_536,
        max_lines: int = 200,
    ) -> OperatorResponse:
        return self._observability_action(
            "logs",
            workspace_root,
            lambda reader: reader.log_snapshot(max_bytes=max_bytes, max_lines=max_lines),
        )

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
            self._ensure_powershell_7()
        except OperatorApiError as error:
            return self._failure(operation, operation_id, error, project_alias=alias)
        except Exception as error:
            return self._unexpected_failure(operation, operation_id, error, project_alias=alias)
        return super()._workspace_action(
            operation,
            workspace_root,
            action,
            extra_args=extra_args,
        )

    def _ensure_powershell_7(self) -> None:
        if self._powershell_major is not None:
            return
        completed = self._run(
            (
                self._powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                POWERSHELL_VERSION_COMMAND,
            ),
            timeout_seconds=min(10.0, self._default_timeout_seconds),
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_RESPONSE,
                "PowerShell version probe returned no output",
                details={"executable": self._powershell},
            )
        try:
            major = int(lines[-1])
        except ValueError as error:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_RESPONSE,
                "PowerShell version probe returned invalid output",
                details={"executable": self._powershell, "stdout_tail": completed.stdout[-1_000:]},
            ) from error
        if major != POWERSHELL_REQUIRED_MAJOR:
            raise OperatorApiError(
                OperatorErrorCode.POWERSHELL_VERSION_UNSUPPORTED,
                "BDB Control Center requires PowerShell 7",
                details={
                    "executable": self._powershell,
                    "required_major": POWERSHELL_REQUIRED_MAJOR,
                    "detected_major": major,
                    "fallback_to_windows_powershell": False,
                },
            )
        self._powershell_major = major

    def _observability_action(
        self,
        operation: str,
        workspace_root: str | Path,
        projection: Callable[[ObservabilityReader], dict[str, Any]],
    ) -> OperatorResponse:
        operation_id = str(uuid4())
        alias: str | None = None
        try:
            reader = ObservabilityReader.from_workspace_root(workspace_root)
            alias = reader.workspace.alias
            data = projection(reader)
            return OperatorResponse.success(
                operation,
                operation_id=operation_id,
                project_alias=alias,
                data=data,
            )
        except OperatorApiError as error:
            return self._failure(operation, operation_id, error, project_alias=alias)
        except Exception as error:  # defensive public boundary
            return self._unexpected_failure(operation, operation_id, error, project_alias=alias)
