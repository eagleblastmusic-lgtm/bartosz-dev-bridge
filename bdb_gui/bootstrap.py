from __future__ import annotations

from pathlib import Path
from typing import Protocol

from bdb_operator import OperatorApi, OperatorResponse

from .state import BootstrapSnapshot, GuiProject


class BootstrapOperator(Protocol):
    def capabilities(self) -> OperatorResponse:
        ...

    def list_projects(self, workspaces_root: str | Path) -> OperatorResponse:
        ...


class BootstrapService:
    """Loads the first GUI snapshot through read-only Operator API methods only."""

    def __init__(self, operator: BootstrapOperator | None = None) -> None:
        self._operator = operator or OperatorApi()

    def load(self, workspaces_root: str | Path) -> BootstrapSnapshot:
        root = str(Path(workspaces_root).expanduser().resolve(strict=False))
        capabilities = self._operator.capabilities()
        if not capabilities.ok:
            return BootstrapSnapshot.failure(
                workspaces_root=root,
                error_code=_error_code(capabilities),
                error_message=_error_message(capabilities),
                operator_schema=capabilities.schema,
            )
        data = capabilities.data
        if data.get("network_listener") is not False:
            return BootstrapSnapshot.failure(
                workspaces_root=root,
                error_code="unsafe_operator_capabilities",
                error_message="Operator API unexpectedly exposes a network listener",
                operator_schema=capabilities.schema,
            )
        if data.get("arbitrary_shell") is not False:
            return BootstrapSnapshot.failure(
                workspaces_root=root,
                error_code="unsafe_operator_capabilities",
                error_message="Operator API unexpectedly exposes arbitrary shell",
                operator_schema=capabilities.schema,
            )

        projects_response = self._operator.list_projects(root)
        if not projects_response.ok:
            return BootstrapSnapshot.failure(
                workspaces_root=root,
                error_code=_error_code(projects_response),
                error_message=_error_message(projects_response),
                operator_schema=capabilities.schema,
            )

        try:
            documents = projects_response.data.get("projects", [])
            if not isinstance(documents, list):
                raise ValueError("Operator projects must be an array")
            projects = [GuiProject.from_operator_document(item) for item in documents]
            invalid_entries = projects_response.data.get("invalid_entries", [])
            if not isinstance(invalid_entries, list):
                raise ValueError("Operator invalid_entries must be an array")
            normalized_invalid: list[dict[str, str]] = []
            for entry in invalid_entries:
                if not isinstance(entry, dict):
                    raise ValueError("Each invalid entry must be an object")
                path = entry.get("path")
                code = entry.get("code")
                if not isinstance(path, str) or not isinstance(code, str):
                    raise ValueError("Invalid entry path and code must be strings")
                normalized_invalid.append({"path": path, "code": code})
        except (TypeError, ValueError) as error:
            return BootstrapSnapshot.failure(
                workspaces_root=root,
                error_code="invalid_operator_response",
                error_message=str(error),
                operator_schema=capabilities.schema,
            )

        return BootstrapSnapshot.success(
            workspaces_root=root,
            projects=projects,
            operator_schema=capabilities.schema,
            operator_transport=str(data.get("transport", "unknown")),
            network_listener=False,
            journal_access=(
                str(data["journal_access"])
                if data.get("journal_access") is not None
                else None
            ),
            invalid_entries=normalized_invalid,
        )


def _error_code(response: OperatorResponse) -> str:
    return response.error.code if response.error is not None else "operator_error"


def _error_message(response: OperatorResponse) -> str:
    return response.error.message if response.error is not None else "Operator API failed"
