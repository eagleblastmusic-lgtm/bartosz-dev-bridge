from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GICLEEAPP_INTEGRATION_SCHEMA = "bdb-gicleeapp-integration-v1"
GICLEEAPP_PREPARE_PLAN_SCHEMA = "bdb-gicleeapp-prepare-plan-v1"
REPOSITORY_FULL_NAME = "eagleblastmusic-lgtm/gicleeart"
DEFAULT_BRANCH_HINT = "master"
PROJECT_ALIAS = "gicleeart"

DEFAULT_ALLOWED_PATHS = (
    "README.md",
    "assets/**",
    "blocks/**",
    "config/settings_schema.json",
    "layout/**",
    "locales/**",
    "sections/**",
    "snippets/**",
    "templates/**",
    "tests/**",
    "scripts/**",
)
FORBIDDEN_SCOPE_HINTS = (
    ".env",
    ".env.*",
    ".git/**",
    "config/settings_data.json",
    "**/*secret*",
    "**/*token*",
    "**/*.pem",
    "**/*.key",
)


def gicleeapp_descriptor() -> dict[str, Any]:
    return {
        "schema": GICLEEAPP_INTEGRATION_SCHEMA,
        "integration_id": "gicleeapp",
        "display_name": "GicleeApp / Shopify theme",
        "repository": {
            "full_name": REPOSITORY_FULL_NAME,
            "default_branch_hint": DEFAULT_BRANCH_HINT,
            "identity_verification_required_before_prepare": True,
        },
        "project": {
            "alias": PROJECT_ALIAS,
            "kind": "shopify_theme",
            "allowed_paths": list(DEFAULT_ALLOWED_PATHS),
            "forbidden_scope_hints": list(FORBIDDEN_SCOPE_HINTS),
        },
        "execution": {
            "plan_only": True,
            "prepare_automatic": False,
            "start_automatic": False,
            "merge_automatic": False,
            "deploy_automatic": False,
            "operator_api_is_execution_boundary": True,
        },
        "ownership": {
            "integration_owner": "DevMaster",
            "application_repository_owner": "GicleeApp",
            "changes_to_application_repository": False,
        },
    }


@dataclass(frozen=True)
class GicleeAppPreparePlan:
    source_repo: str
    workspace_root: str
    observed_branch: str
    python_executable: str
    test_timeout_seconds: int
    allowed_paths: tuple[str, ...] = DEFAULT_ALLOWED_PATHS
    alias: str = PROJECT_ALIAS
    repository_full_name: str = REPOSITORY_FULL_NAME
    default_branch_hint: str = DEFAULT_BRANCH_HINT
    repository_identity_verification: str = "external_required"
    requires_confirmation: bool = True
    read_only: bool = True
    mutation_operations_invoked: int = 0
    schema: str = GICLEEAPP_PREPARE_PLAN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "repository_full_name": self.repository_full_name,
            "default_branch_hint": self.default_branch_hint,
            "repository_identity_verification": self.repository_identity_verification,
            "alias": self.alias,
            "source_repo": self.source_repo,
            "workspace_root": self.workspace_root,
            "observed_branch": self.observed_branch,
            "allowed_paths": list(self.allowed_paths),
            "python_executable": self.python_executable,
            "test_timeout_seconds": self.test_timeout_seconds,
            "requires_confirmation": self.requires_confirmation,
            "read_only": self.read_only,
            "mutation_operations_invoked": self.mutation_operations_invoked,
        }

    def operator_prepare_parameters(self) -> dict[str, Any]:
        """Return only the parameters supported by public OperatorApi.prepare()."""
        return {
            "workspace_root": self.workspace_root,
            "source_repo": self.source_repo,
            "alias": self.alias,
            "allowed_paths": self.allowed_paths,
            "test_timeout_seconds": self.test_timeout_seconds,
            "python_executable": self.python_executable,
        }


class GicleeAppIntegration:
    """Builds a non-mutating Prepare proposal for an existing local checkout."""

    def build_prepare_plan(
        self,
        *,
        source_repo: str | Path,
        workspaces_root: str | Path,
        python_executable: str | Path | None = None,
        test_timeout_seconds: int = 120,
    ) -> GicleeAppPreparePlan:
        source = Path(source_repo).expanduser().resolve(strict=False)
        git_dir = _resolve_git_dir(source)
        observed_branch = _attached_branch(git_dir)

        workspaces = Path(workspaces_root).expanduser().resolve(strict=False)
        if not workspaces.is_dir():
            raise ValueError("workspaces_root must be an existing directory")
        workspace = (workspaces / PROJECT_ALIAS).resolve(strict=False)
        try:
            workspace.relative_to(workspaces)
        except ValueError as error:
            raise ValueError("workspace_root must stay inside workspaces_root") from error
        if workspace.exists():
            raise ValueError("GicleeApp workspace already exists")
        if source == workspace or source in workspace.parents:
            raise ValueError("workspace must stay outside source checkout")

        python = Path(python_executable or sys.executable).expanduser().resolve(strict=False)
        if not python.is_file():
            raise ValueError("python_executable must be an existing file")
        if isinstance(test_timeout_seconds, bool) or not isinstance(test_timeout_seconds, int):
            raise ValueError("test_timeout_seconds must be an integer")
        if not 1 <= test_timeout_seconds <= 3600:
            raise ValueError("test_timeout_seconds must be between 1 and 3600")

        return GicleeAppPreparePlan(
            source_repo=str(source),
            workspace_root=str(workspace),
            observed_branch=observed_branch,
            python_executable=str(python),
            test_timeout_seconds=test_timeout_seconds,
        )


def _resolve_git_dir(source: Path) -> Path:
    if not source.is_dir():
        raise ValueError("source_repo must be an existing directory")
    marker = source / ".git"
    if marker.is_dir():
        return marker.resolve()
    if marker.is_file():
        first_line = marker.read_text(encoding="utf-8-sig").splitlines()[0].strip()
        if not first_line.lower().startswith("gitdir:"):
            raise ValueError("source_repo .git file is invalid")
        candidate = Path(first_line.split(":", 1)[1].strip())
        if not candidate.is_absolute():
            candidate = (source / candidate).resolve(strict=False)
        if not candidate.is_dir():
            raise ValueError("source_repo gitdir does not exist")
        return candidate
    raise ValueError("source_repo must be a non-bare Git checkout")


def _attached_branch(git_dir: Path) -> str:
    head = git_dir / "HEAD"
    if not head.is_file():
        raise ValueError("source_repo Git HEAD is missing")
    value = head.read_text(encoding="utf-8-sig").strip()
    prefix = "ref: refs/heads/"
    if not value.startswith(prefix):
        raise ValueError("source_repo must be attached to a local branch")
    branch = value[len(prefix):].strip()
    if not branch or branch.startswith("/") or ".." in branch:
        raise ValueError("source_repo branch reference is invalid")
    return branch
