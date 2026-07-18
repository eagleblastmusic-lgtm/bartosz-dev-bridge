from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bdb_gui.projects import ProjectPrepareService
from bdb_operator.models import OperatorError, OperatorResponse


class FakePrepareOperator:
    def __init__(self, response: OperatorResponse | None = None) -> None:
        self.response = response or OperatorResponse.success(
            "prepare",
            project_alias="alpha",
            operation_id="prepare-op",
            data={"status": "prepared", "workspace_root": "C:/workspaces/alpha"},
        )
        self.calls: list[dict[str, object]] = []

    def prepare(self, workspace_root: str | Path, **kwargs: object) -> OperatorResponse:
        self.calls.append({"workspace_root": str(workspace_root), **kwargs})
        return self.response


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    return source


def test_build_plan_is_read_only_and_normalizes_paths(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    source = make_source(tmp_path)
    service = ProjectPrepareService(FakePrepareOperator())

    plan = service.build_plan(
        workspaces_root=workspaces,
        alias="alpha",
        source_repo=source,
        allowed_paths=["README.md", "tests\\*.py", "README.md", "  src/**  "],
        python_executable=sys.executable,
    )

    assert plan.alias == "alpha"
    assert plan.workspace_root == str((workspaces / "alpha").resolve())
    assert plan.source_repo == str(source.resolve())
    assert plan.allowed_paths == ("README.md", "tests/*.py", "src/**")
    assert plan.requires_confirmation is True
    assert plan.preflight_owner == "existing_prepare_workspace_loop"
    assert plan.read_only is True
    assert plan.mutation_operations_invoked == 0
    assert not (workspaces / "alpha").exists()
    assert plan.to_dict()["schema"] == "bdb-gui-prepare-plan-v1"


def test_execute_routes_exact_validated_plan_to_operator(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    source = make_source(tmp_path)
    operator = FakePrepareOperator()
    service = ProjectPrepareService(operator)
    plan = service.build_plan(
        workspaces_root=workspaces,
        alias="alpha",
        source_repo=source,
        allowed_paths=["README.md", "tests/*.py"],
        python_executable=sys.executable,
        test_timeout_seconds=90,
        max_patch_bytes=100_000,
        max_changed_files=7,
        auto_send_max_bytes=12_000,
        worker_timeout_seconds=180,
    )

    result = service.execute(plan)

    assert result.ok is True
    assert result.project_alias == "alpha"
    assert result.operation_id == "prepare-op"
    assert result.mutation_operations_invoked == 1
    assert operator.calls == [
        {
            "workspace_root": plan.workspace_root,
            "source_repo": plan.source_repo,
            "alias": "alpha",
            "allowed_paths": ("README.md", "tests/*.py"),
            "native_config": None,
            "test_timeout_seconds": 90,
            "max_patch_bytes": 100_000,
            "max_changed_files": 7,
            "auto_send_max_bytes": 12_000,
            "worker_timeout_seconds": 180,
            "python_executable": plan.python_executable,
        }
    ]
    assert result.to_dict()["plan"]["read_only"] is True


def test_prepare_failure_is_preserved(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    source = make_source(tmp_path)
    response = OperatorResponse.failure(
        "prepare",
        project_alias="alpha",
        operation_id="prepare-failed",
        error=OperatorError(code="operator_failed", message="source checkout is dirty"),
    )
    operator = FakePrepareOperator(response)
    service = ProjectPrepareService(operator)
    plan = service.build_plan(
        workspaces_root=workspaces,
        alias="alpha",
        source_repo=source,
        allowed_paths=["README.md"],
    )

    result = service.execute(plan)

    assert result.ok is False
    assert result.error_code == "operator_failed"
    assert result.error_message == "source checkout is dirty"
    assert result.mutation_operations_invoked == 1


@pytest.mark.parametrize(
    "alias",
    ["", "../alpha", "alpha/beta", "alpha beta", "_alpha", "a" * 65],
)
def test_invalid_alias_is_rejected_before_operator(tmp_path: Path, alias: str) -> None:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    source = make_source(tmp_path)
    operator = FakePrepareOperator()

    with pytest.raises(ValueError, match="alias"):
        ProjectPrepareService(operator).build_plan(
            workspaces_root=workspaces,
            alias=alias,
            source_repo=source,
            allowed_paths=["README.md"],
        )

    assert operator.calls == []


@pytest.mark.parametrize(
    "allowed_paths",
    [[], [""], ["../secret"], ["src/../secret"], ["C:/secret"], ["/absolute"]],
)
def test_invalid_allowed_paths_are_rejected(
    tmp_path: Path,
    allowed_paths: list[str],
) -> None:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    source = make_source(tmp_path)

    with pytest.raises(ValueError):
        ProjectPrepareService().build_plan(
            workspaces_root=workspaces,
            alias="alpha",
            source_repo=source,
            allowed_paths=allowed_paths,
        )


def test_existing_workspace_and_non_git_source_are_rejected(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    (workspaces / "alpha").mkdir()
    source = tmp_path / "not-git"
    source.mkdir()
    service = ProjectPrepareService()

    with pytest.raises(ValueError, match="workspace_root already exists"):
        service.build_plan(
            workspaces_root=workspaces,
            alias="alpha",
            source_repo=make_source(tmp_path / "other"),
            allowed_paths=["README.md"],
        )

    with pytest.raises(ValueError, match="Git checkout"):
        service.build_plan(
            workspaces_root=workspaces,
            alias="beta",
            source_repo=source,
            allowed_paths=["README.md"],
        )


def test_execute_requires_prepare_plan_instance() -> None:
    with pytest.raises(TypeError, match="validated PreparePlan"):
        ProjectPrepareService().execute({})  # type: ignore[arg-type]
