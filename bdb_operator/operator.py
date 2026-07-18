from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .api import OperatorApi as ExecutionOperatorApi
from .errors import OperatorApiError
from .models import OperatorResponse
from .observability import ObservabilityReader


class OperatorApi(ExecutionOperatorApi):
    """Public Operator API including P03 execution and P04 read projections."""

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
