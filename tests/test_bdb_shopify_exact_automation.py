from __future__ import annotations

import inspect
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge.execution import ExecutionCoordinator
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_manager import WorkspaceManager
import bdb_bridge.workspace_promoter as workspace_promoter


def test_replace_exact_accepts_shopify_theme_check() -> None:
    document = {
        "operation": "replace_exact_and_test",
        "payload": {
            "profile_id": "shopify_theme_check",
        },
    }

    payload, operation, profile_id = ExecutionCoordinator._parse_command(
        json.dumps(document)
    )

    assert payload == document["payload"]
    assert operation == "replace_exact_and_test"
    assert profile_id == "shopify_theme_check"


def test_single_file_temp_keeps_real_extension_last(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    templates = root / "templates"
    templates.mkdir(parents=True)

    manager = object.__new__(WorkspaceManager)
    manager.path = root
    manager.config = SimpleNamespace(
        allowed_paths=("templates/**",),
    )
    manager.manifest_paths = ("templates/**",)

    plan = SimpleNamespace(
        target_path="templates/page.filozofia-marki.json",
        plan_sha256="sha256:" + ("a" * 64),
    )

    temp = manager.temp_path_for(plan)

    assert temp.name == (
        ".bdb_temp_page.filozofia-marki_aaaaaaaaaaaaaaaa.json"
    )
    assert temp.suffix == ".json"
    assert temp.parent != templates
    assert ".bdb-temp" in temp.parts


def _promoter_type() -> type:
    matches = [
        value
        for value in vars(workspace_promoter).values()
        if inspect.isclass(value)
        and "_validate_result" in value.__dict__
    ]

    assert len(matches) == 1
    return matches[0]


def test_promoter_accepts_successful_exact_replace() -> None:
    promoter_type = _promoter_type()
    promoter = object.__new__(promoter_type)
    promoter.config = SimpleNamespace(
        allowed_paths=("templates/**",),
    )

    session_id = str(uuid.uuid4())
    document = {
        "status": "success",
        "exit_code": 0,
        "session_id": session_id,
        "sequence": 1,
        "data": {
            "operation": "replace_exact_and_test",
            "rollback_performed": False,
        },
        "changed_files": [
            "templates/page.filozofia-marki.json",
        ],
    }

    result = promoter._validate_result(document)

    assert result == (
        session_id,
        1,
        ("templates/page.filozofia-marki.json",),
    )


def test_promoter_rejects_exact_replace_with_rollback() -> None:
    promoter_type = _promoter_type()
    promoter = object.__new__(promoter_type)
    promoter.config = SimpleNamespace(
        allowed_paths=("templates/**",),
    )

    document = {
        "status": "success",
        "exit_code": 0,
        "session_id": str(uuid.uuid4()),
        "sequence": 1,
        "data": {
            "operation": "replace_exact_and_test",
            "rollback_performed": True,
        },
        "changed_files": [
            "templates/page.filozofia-marki.json",
        ],
    }

    with pytest.raises(BridgeError):
        promoter._validate_result(document)
