from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from bdb_bridge.shopify_theme_check_profile import (
    compare_theme_check_documents,
    run_shopify_theme_check_profile,
)


def _document(root: Path, offenses: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "path": str(root / "sections" / "hero.liquid"),
            "offenses": offenses,
        }
    ]


def _offense(
    *,
    check: str = "ParserBlockingScript",
    message: str = "Avoid parser blocking scripts",
    severity: str = "error",
    line: int = 10,
) -> dict[str, object]:
    return {
        "check": check,
        "message": message,
        "severity": severity,
        "start_row": line,
    }


def test_compare_ignores_existing_blocking_errors(tmp_path: Path) -> None:
    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    baseline_root.mkdir()
    current_root.mkdir()

    comparison = compare_theme_check_documents(
        _document(baseline_root, [_offense(line=10)]),
        _document(current_root, [_offense(line=99)]),
        baseline_root=baseline_root,
        current_root=current_root,
    )

    assert comparison["baseline_blocking_errors"] == 1
    assert comparison["current_blocking_errors"] == 1
    assert comparison["new_blocking_errors"] == 0
    assert comparison["new_errors"] == []


def test_compare_detects_additional_occurrence_of_same_error(tmp_path: Path) -> None:
    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    baseline_root.mkdir()
    current_root.mkdir()

    comparison = compare_theme_check_documents(
        _document(baseline_root, [_offense(line=10)]),
        _document(current_root, [_offense(line=10), _offense(line=20)]),
        baseline_root=baseline_root,
        current_root=current_root,
    )

    assert comparison["new_blocking_errors"] == 1
    assert len(comparison["new_errors"]) == 1


def test_compare_ignores_warnings(tmp_path: Path) -> None:
    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    baseline_root.mkdir()
    current_root.mkdir()

    comparison = compare_theme_check_documents(
        _document(baseline_root, []),
        _document(current_root, [_offense(severity="warning")]),
        baseline_root=baseline_root,
        current_root=current_root,
    )

    assert comparison["current_blocking_errors"] == 0
    assert comparison["new_blocking_errors"] == 0


def test_profile_succeeds_when_current_matches_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    baseline_root.mkdir()
    current_root.mkdir()

    @contextmanager
    def fake_snapshots(*_args: object, **_kwargs: object):
        yield baseline_root, current_root, "a" * 40

    def fake_check(
        _command: object,
        root: Path,
        **_kwargs: object,
    ) -> tuple[object, int, str]:
        return _document(root, [_offense()]), 1, ""

    monkeypatch.setattr(
        "bdb_bridge.shopify_theme_check_profile._theme_snapshots",
        fake_snapshots,
    )
    monkeypatch.setattr(
        "bdb_bridge.shopify_theme_check_profile._run_theme_check_document",
        fake_check,
    )

    outcome = run_shopify_theme_check_profile(
        workspace_path=tmp_path,
        command=("shopify.cmd", "theme", "check"),
        timeout_seconds=30,
        environment={"PATH": ""},
    )

    assert outcome.status == "success"
    assert outcome.exit_code == 0
    summary = json.loads(outcome.stdout)
    assert summary["baseline_blocking_errors"] == 1
    assert summary["current_blocking_errors"] == 1
    assert summary["new_blocking_errors"] == 0


def test_profile_fails_only_for_new_blocking_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    baseline_root.mkdir()
    current_root.mkdir()

    @contextmanager
    def fake_snapshots(*_args: object, **_kwargs: object):
        yield baseline_root, current_root, "b" * 40

    def fake_check(
        _command: object,
        root: Path,
        **_kwargs: object,
    ) -> tuple[object, int, str]:
        offenses = [] if root == baseline_root else [_offense()]
        return _document(root, offenses), 1 if offenses else 0, ""

    monkeypatch.setattr(
        "bdb_bridge.shopify_theme_check_profile._theme_snapshots",
        fake_snapshots,
    )
    monkeypatch.setattr(
        "bdb_bridge.shopify_theme_check_profile._run_theme_check_document",
        fake_check,
    )

    outcome = run_shopify_theme_check_profile(
        workspace_path=tmp_path,
        command=("shopify.cmd", "theme", "check"),
        timeout_seconds=30,
        environment={"PATH": ""},
    )

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    summary = json.loads(outcome.stdout)
    assert summary["new_blocking_errors"] == 1
    assert summary["new_errors"][0]["path"] == "sections/hero.liquid"


def test_profile_reports_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def fake_snapshots(*_args: object, **_kwargs: object):
        raise subprocess.TimeoutExpired("git", 1)
        yield

    monkeypatch.setattr(
        "bdb_bridge.shopify_theme_check_profile._theme_snapshots",
        fake_snapshots,
    )

    outcome = run_shopify_theme_check_profile(
        workspace_path=tmp_path,
        command=("shopify.cmd", "theme", "check"),
        timeout_seconds=1,
        environment={"PATH": ""},
    )

    assert outcome.status == "timeout"
    assert outcome.exit_code is None
    assert "timed out" in outcome.stderr


def test_profile_source_contains_only_fixed_theme_roots() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "bdb_bridge"
        / "shopify_theme_check_profile.py"
    ).read_text(encoding="utf-8")

    for root in (
        "assets",
        "blocks",
        "config",
        "layout",
        "locales",
        "sections",
        "snippets",
        "templates",
    ):
        assert f'"{root}"' in source
    assert "cursor-api" not in source
    assert "_live_" not in source
    assert "shell=True" not in source
