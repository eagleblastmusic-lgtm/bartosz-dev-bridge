from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


GUI_BOOTSTRAP_SCHEMA = "bdb-gui-bootstrap-v1"
GUI_PROJECT_SCHEMA = "bdb-gui-project-v1"


@dataclass(frozen=True, order=True)
class GuiProject:
    alias: str
    workspace_root: str
    source_repo: str
    source_branch: str
    configured_status: str
    allowed_paths: tuple[str, ...] = field(default_factory=tuple, compare=False)
    schema: str = field(default=GUI_PROJECT_SCHEMA, compare=False)

    @classmethod
    def from_operator_document(cls, document: dict[str, Any]) -> "GuiProject":
        required = (
            "alias",
            "workspace_root",
            "source_repo",
            "source_branch",
            "configured_status",
        )
        for key in required:
            value = document.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Project field is missing or invalid: {key}")
        allowed = document.get("allowed_paths", [])
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("Project allowed_paths must be an array of strings")
        return cls(
            alias=document["alias"],
            workspace_root=document["workspace_root"],
            source_repo=document["source_repo"],
            source_branch=document["source_branch"],
            configured_status=document["configured_status"],
            allowed_paths=tuple(allowed),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "alias": self.alias,
            "workspace_root": self.workspace_root,
            "source_repo": self.source_repo,
            "source_branch": self.source_branch,
            "configured_status": self.configured_status,
            "allowed_paths": list(self.allowed_paths),
        }


@dataclass(frozen=True)
class BootstrapSnapshot:
    workspaces_root: str
    projects: tuple[GuiProject, ...]
    operator_schema: str | None
    operator_transport: str | None
    network_listener: bool | None
    journal_access: str | None
    invalid_entries: tuple[dict[str, str], ...] = field(default_factory=tuple)
    error_code: str | None = None
    error_message: str | None = None
    read_only: bool = True
    mutation_operations_invoked: int = 0
    schema: str = GUI_BOOTSTRAP_SCHEMA

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @classmethod
    def success(
        cls,
        *,
        workspaces_root: str,
        projects: Iterable[GuiProject],
        operator_schema: str,
        operator_transport: str,
        network_listener: bool,
        journal_access: str | None,
        invalid_entries: Iterable[dict[str, str]] = (),
    ) -> "BootstrapSnapshot":
        return cls(
            workspaces_root=workspaces_root,
            projects=tuple(sorted(projects)),
            operator_schema=operator_schema,
            operator_transport=operator_transport,
            network_listener=network_listener,
            journal_access=journal_access,
            invalid_entries=tuple(dict(item) for item in invalid_entries),
        )

    @classmethod
    def failure(
        cls,
        *,
        workspaces_root: str,
        error_code: str,
        error_message: str,
        operator_schema: str | None = None,
    ) -> "BootstrapSnapshot":
        return cls(
            workspaces_root=workspaces_root,
            projects=(),
            operator_schema=operator_schema,
            operator_transport=None,
            network_listener=None,
            journal_access=None,
            error_code=error_code,
            error_message=error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workspaces_root": self.workspaces_root,
            "ok": self.ok,
            "read_only": self.read_only,
            "mutation_operations_invoked": self.mutation_operations_invoked,
            "operator": {
                "schema": self.operator_schema,
                "transport": self.operator_transport,
                "network_listener": self.network_listener,
                "journal_access": self.journal_access,
            },
            "projects": [project.to_dict() for project in self.projects],
            "invalid_entries": [dict(item) for item in self.invalid_entries],
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }
