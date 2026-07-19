from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bdb_integrations import GicleeAppIntegration, gicleeapp_descriptor
from bdb_integrations.gicleeapp import DEFAULT_ALLOWED_PATHS, FORBIDDEN_SCOPE_HINTS


def checkout(tmp_path: Path, *, branch: str = "master") -> Path:
    source = tmp_path / "gicleeart"
    git_dir = source / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    (source / "layout").mkdir()
    (source / "layout" / "theme.liquid").write_text("<!doctype html>\n", encoding="utf-8")
    return source


def test_descriptor_identifies_canonical_repository_and_safe_scope() -> None:
    descriptor = gicleeapp_descriptor()

    assert descriptor["schema"] == "bdb-gicleeapp-integration-v1"
    assert descriptor["repository"]["full_name"] == "eagleblastmusic-lgtm/gicleeart"
    assert descriptor["repository"]["default_branch_hint"] == "master"
    assert descriptor["repository"]["identity_verification_required_before_prepare"] is True
    assert descriptor["project"]["alias"] == "gicleeart"
    assert descriptor["project"]["kind"] == "shopify_theme"
    assert descriptor["execution"]["plan_only"] is True
    assert descriptor["execution"]["prepare_automatic"] is False
    assert descriptor["execution"]["merge_automatic"] is False
    assert descriptor["execution"]["deploy_automatic"] is False
    assert descriptor["ownership"]["changes_to_application_repository"] is False
    assert "config/settings_data.json" not in DEFAULT_ALLOWED_PATHS
    assert "config/**" not in DEFAULT_ALLOWED_PATHS
    assert "config/settings_data.json" in FORBIDDEN_SCOPE_HINTS
    assert ".env" in FORBIDDEN_SCOPE_HINTS


def test_build_prepare_plan_is_read_only_and_does_not_create_workspace(tmp_path: Path) -> None:
    source = checkout(tmp_path)
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    plan = GicleeAppIntegration().build_prepare_plan(
        source_repo=source,
        workspaces_root=workspaces,
        python_executable=sys.executable,
        test_timeout_seconds=180,
    )

    assert plan.repository_full_name == "eagleblastmusic-lgtm/gicleeart"
    assert plan.default_branch_hint == "master"
    assert plan.repository_identity_verification == "external_required"
    assert plan.alias == "gicleeart"
    assert plan.observed_branch == "master"
    assert plan.allowed_paths == DEFAULT_ALLOWED_PATHS
    assert plan.read_only is True
    assert plan.mutation_operations_invoked == 0
    assert plan.requires_confirmation is True
    assert not (workspaces / "gicleeart").exists()


def test_prepare_parameters_match_public_operator_api_contract(tmp_path: Path) -> None:
    source = checkout(tmp_path, branch="feature/catalog")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    plan = GicleeAppIntegration().build_prepare_plan(
        source_repo=source,
        workspaces_root=workspaces,
        python_executable=sys.executable,
        test_timeout_seconds=90,
    )

    assert plan.observed_branch == "feature/catalog"
    assert plan.operator_prepare_parameters() == {
        "workspace_root": str((workspaces / "gicleeart").resolve()),
        "source_repo": str(source.resolve()),
        "alias": "gicleeart",
        "allowed_paths": DEFAULT_ALLOWED_PATHS,
        "test_timeout_seconds": 90,
        "python_executable": str(Path(sys.executable).resolve()),
    }


def test_worktree_git_file_is_supported(tmp_path: Path) -> None:
    source = tmp_path / "worktree"
    source.mkdir()
    actual_git = tmp_path / "gitdirs" / "worktree"
    actual_git.mkdir(parents=True)
    (actual_git / "HEAD").write_text("ref: refs/heads/gpt-work/test\n", encoding="utf-8")
    relative = actual_git.relative_to(source.parent)
    (source / ".git").write_text(f"gitdir: ../{relative.as_posix()}\n", encoding="utf-8")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    plan = GicleeAppIntegration().build_prepare_plan(
        source_repo=source,
        workspaces_root=workspaces,
    )

    assert plan.observed_branch == "gpt-work/test"


def test_detached_or_missing_checkout_is_rejected(tmp_path: Path) -> None:
    source = checkout(tmp_path)
    (source / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    with pytest.raises(ValueError, match="attached"):
        GicleeAppIntegration().build_prepare_plan(
            source_repo=source,
            workspaces_root=workspaces,
        )

    with pytest.raises(ValueError, match="Git checkout"):
        GicleeAppIntegration().build_prepare_plan(
            source_repo=tmp_path / "missing",
            workspaces_root=workspaces,
        )


def test_existing_workspace_and_invalid_timeout_are_rejected(tmp_path: Path) -> None:
    source = checkout(tmp_path)
    workspaces = tmp_path / "workspaces"
    (workspaces / "gicleeart").mkdir(parents=True)

    with pytest.raises(ValueError, match="already exists"):
        GicleeAppIntegration().build_prepare_plan(
            source_repo=source,
            workspaces_root=workspaces,
        )

    (workspaces / "gicleeart").rmdir()
    with pytest.raises(ValueError, match="between 1 and 3600"):
        GicleeAppIntegration().build_prepare_plan(
            source_repo=source,
            workspaces_root=workspaces,
            test_timeout_seconds=0,
        )
