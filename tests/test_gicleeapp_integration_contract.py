from __future__ import annotations

import ast
import json
from pathlib import Path

from bdb_integrations import gicleeapp_descriptor


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "bdb_integrations" / "gicleeapp.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p15_artifacts_exist() -> None:
    expected = (
        ROOT / "bdb_integrations" / "__init__.py",
        MODULE,
        ROOT / "integrations" / "gicleeapp.json",
        ROOT / "schemas" / "bdb-gicleeapp-integration-v1.schema.json",
        ROOT / "schemas" / "bdb-gicleeapp-prepare-plan-v1.schema.json",
        ROOT / "docs" / "BDB_GICLEEAPP_INTEGRATION.md",
        ROOT / "docs" / "adr" / "0016-plan-only-gicleeapp-integration.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing P15 artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_static_descriptor_matches_generated_contract() -> None:
    static = json.loads(read(ROOT / "integrations" / "gicleeapp.json"))
    assert static == gicleeapp_descriptor()


def test_schemas_are_closed_and_never_claim_automatic_execution() -> None:
    integration = json.loads(read(ROOT / "schemas" / "bdb-gicleeapp-integration-v1.schema.json"))
    plan = json.loads(read(ROOT / "schemas" / "bdb-gicleeapp-prepare-plan-v1.schema.json"))

    assert integration["additionalProperties"] is False
    assert integration["properties"]["repository"]["additionalProperties"] is False
    execution = integration["properties"]["execution"]["properties"]
    assert execution["plan_only"] == {"const": True}
    assert execution["prepare_automatic"] == {"const": False}
    assert execution["start_automatic"] == {"const": False}
    assert execution["merge_automatic"] == {"const": False}
    assert execution["deploy_automatic"] == {"const": False}
    assert plan["additionalProperties"] is False
    assert plan["properties"]["repository_identity_verification"] == {"const": "external_required"}
    assert plan["properties"]["mutation_operations_invoked"] == {"const": 0}


def test_integration_has_no_operator_git_network_or_write_surface() -> None:
    source = read(MODULE)
    tree = ast.parse(source)
    forbidden_roots = {
        "subprocess", "socket", "socketserver", "http", "urllib", "requests",
        "aiohttp", "websockets", "sqlite3", "shutil",
    }
    forbidden_calls = {"prepare", "start", "stop", "rearm", "write_text", "write_bytes", "open"}
    observed_calls: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".", 1)[0]}
        else:
            roots = set()
        assert forbidden_roots.isdisjoint(roots)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("bdb_operator", "bdb_bridge"))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                observed_calls.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                observed_calls.add(node.func.id)

    assert forbidden_calls.isdisjoint(observed_calls)
    lowered = source.lower()
    for token in ("operatorapi(", "git.exe", "powershell.exe", "shell=true"):
        assert token not in lowered


def test_default_scope_excludes_store_state_and_secret_patterns() -> None:
    descriptor = gicleeapp_descriptor()
    allowed = descriptor["project"]["allowed_paths"]
    forbidden = descriptor["project"]["forbidden_scope_hints"]

    assert "config/settings_schema.json" in allowed
    assert "config/**" not in allowed
    assert "config/settings_data.json" not in allowed
    assert "config/settings_data.json" in forbidden
    assert ".env" in forbidden
    assert "**/*.key" in forbidden
    assert "**/*.pem" in forbidden


def test_descriptor_does_not_claim_application_repository_changes() -> None:
    descriptor = gicleeapp_descriptor()
    assert descriptor["ownership"]["integration_owner"] == "DevMaster"
    assert descriptor["ownership"]["application_repository_owner"] == "GicleeApp"
    assert descriptor["ownership"]["changes_to_application_repository"] is False
