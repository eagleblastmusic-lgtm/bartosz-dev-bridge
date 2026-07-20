from __future__ import annotations

import ast
import json
from pathlib import Path

from bdb_bartosz_os import module_manifest
from bdb_bartosz_os.manifest import MUTATION_OPERATIONS, READ_OPERATIONS


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "bdb_bartosz_os"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p14_artifacts_exist() -> None:
    expected = (
        PACKAGE / "__init__.py",
        PACKAGE / "manifest.py",
        PACKAGE / "adapter.py",
        ROOT / "manifests" / "bartosz-dev-bridge.module.json",
        ROOT / "schemas" / "bartosz-os-module-manifest-v1.schema.json",
        ROOT / "schemas" / "bdb-bartosz-os-request-v1.schema.json",
        ROOT / "schemas" / "bdb-bartosz-os-response-v1.schema.json",
        ROOT / "docs" / "BDB_BARTOSZ_OS_ADAPTER.md",
        ROOT / "docs" / "adr" / "0015-stateless-bartosz-os-adapter.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing P14 artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_manifest_declares_real_ownership_and_safety_boundaries() -> None:
    manifest = module_manifest()
    static = json.loads(read(ROOT / "manifests" / "bartosz-dev-bridge.module.json"))

    assert manifest["schema"] == "bartosz-os-module-manifest-v1"
    assert manifest["module_id"] == "devmaster.bartosz-dev-bridge"
    assert manifest["owner_module"] == "DevMaster"
    assert manifest["source_repository"] == "eagleblastmusic-lgtm/bartosz-dev-bridge"
    assert manifest["transport"] == {
        "kind": "in_process",
        "local_only": True,
        "network_listener": False,
    }
    assert manifest["operations"]["arbitrary_shell"] is False
    assert manifest["operations"]["auto_merge"] is False
    assert manifest["operations"]["auto_deploy"] is False
    assert manifest["mutation_policy"]["adapter_default"] == "disabled"
    assert manifest["mutation_policy"]["requires_request_authorization"] is True
    assert manifest["state"]["adapter_persists_state"] is False
    assert manifest["state"]["github_is_code_source_of_truth"] is True
    assert manifest["state"]["bartosz_os_core_is_source_of_truth"] is False
    assert {key: value for key, value in manifest.items() if key != "version"} == {
        key: value for key, value in static.items() if key != "version"
    }


def test_0_3_0_manifest_exposes_sessions_and_versioned_read_contracts() -> None:
    manifest = module_manifest()

    assert READ_OPERATIONS == (
        "capabilities",
        "list_projects",
        "status",
        "events",
        "current_operation",
        "sessions",
        "logs",
    )
    assert MUTATION_OPERATIONS == ("prepare", "start", "stop", "rearm")
    assert manifest["operations"]["read"] == list(READ_OPERATIONS)
    assert manifest["operations"]["mutation"] == list(MUTATION_OPERATIONS)
    assert manifest["contracts"] == {
        "request": "bdb-bartosz-os-request-v1",
        "response": "bdb-bartosz-os-response-v1",
        "operator_response": "bdb-operator-response-v1",
        "event": "bdb-event-v1",
        "session_history": "bdb-session-history-v1",
        "repair_correlation": "bdb-repair-correlation-v1",
        "repair_group": "bdb-repair-group-v1",
        "control_center_smoke": "bdb-control-center-smoke-v1",
        "release_manifest": "bdb-release-manifest-v1",
    }


def test_manifest_and_adapter_schemas_are_closed() -> None:
    module_schema = json.loads(read(ROOT / "schemas" / "bartosz-os-module-manifest-v1.schema.json"))
    request_schema = json.loads(read(ROOT / "schemas" / "bdb-bartosz-os-request-v1.schema.json"))
    response_schema = json.loads(read(ROOT / "schemas" / "bdb-bartosz-os-response-v1.schema.json"))

    assert module_schema["additionalProperties"] is False
    assert module_schema["properties"]["transport"]["additionalProperties"] is False
    assert module_schema["properties"]["operations"]["additionalProperties"] is False
    assert module_schema["properties"]["operations"]["properties"]["read"]["const"] == list(READ_OPERATIONS)
    contracts = module_schema["properties"]["contracts"]
    assert contracts["additionalProperties"] is False
    assert set(contracts["required"]) == {
        "request",
        "response",
        "operator_response",
        "event",
        "session_history",
        "repair_correlation",
        "repair_group",
        "control_center_smoke",
        "release_manifest",
    }
    assert request_schema["additionalProperties"] is False
    assert response_schema["additionalProperties"] is False
    assert response_schema["properties"]["adapter_persisted_state"] == {"const": False}
    assert response_schema["properties"]["network_listener"] == {"const": False}


def test_adapter_has_no_listener_persistence_or_direct_core_access() -> None:
    source = read(PACKAGE / "adapter.py")
    tree = ast.parse(source)
    forbidden_roots = {
        "sqlite3", "subprocess", "socket", "socketserver", "http", "urllib",
        "requests", "aiohttp", "websockets", "fastapi", "flask",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".", 1)[0]}
        else:
            continue
        assert forbidden_roots.isdisjoint(roots)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("bdb_bridge")

    lowered = source.lower()
    for token in (
        "listen(", "bind(", "connect(", "open(", "write_text", "write_bytes",
        "git.exe", "powershell.exe", "shell=true",
    ):
        assert token not in lowered
    assert "from bdb_operator import OperatorApi, OperatorResponse" in source


def test_adapter_catalog_is_closed_and_mutations_default_off() -> None:
    source = read(PACKAGE / "adapter.py")
    manifest = read(PACKAGE / "manifest.py")
    assert 'mutations_enabled: bool = False' in source
    assert '"mutation_adapter_disabled"' in source
    assert '"mutation_authorization_required"' in source
    assert 'MUTATION_OPERATIONS = ("prepare", "start", "stop", "rearm")' in manifest
    assert 'if operation == "sessions":' in source
    assert "operation is outside the closed adapter catalog" in source
