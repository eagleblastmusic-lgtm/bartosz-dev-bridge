from __future__ import annotations

from importlib import metadata
from typing import Any


MODULE_MANIFEST_SCHEMA = "bartosz-os-module-manifest-v1"
READ_OPERATIONS = (
    "capabilities",
    "list_projects",
    "status",
    "events",
    "current_operation",
    "sessions",
    "logs",
)
MUTATION_OPERATIONS = ("prepare", "start", "stop", "rearm")


def module_manifest() -> dict[str, Any]:
    try:
        version = metadata.version("bartosz-dev-bridge")
    except metadata.PackageNotFoundError:
        version = "source-tree"
    return {
        "schema": MODULE_MANIFEST_SCHEMA,
        "module_id": "devmaster.bartosz-dev-bridge",
        "display_name": "Bartosz Dev Bridge",
        "version": version,
        "responsibility": "local_code_edit_test_promote_bridge",
        "owner_module": "DevMaster",
        "source_repository": "eagleblastmusic-lgtm/bartosz-dev-bridge",
        "transport": {
            "kind": "in_process",
            "local_only": True,
            "network_listener": False,
        },
        "operations": {
            "read": list(READ_OPERATIONS),
            "mutation": list(MUTATION_OPERATIONS),
            "arbitrary_shell": False,
            "auto_merge": False,
            "auto_deploy": False,
        },
        "mutation_policy": {
            "adapter_default": "disabled",
            "requires_adapter_enablement": True,
            "requires_request_authorization": True,
            "implicit_mutation": False,
        },
        "state": {
            "adapter_persists_state": False,
            "operator_api_is_execution_boundary": True,
            "github_is_code_source_of_truth": True,
            "bartosz_os_core_is_source_of_truth": False,
        },
        "contracts": {
            "request": "bdb-bartosz-os-request-v1",
            "response": "bdb-bartosz-os-response-v1",
            "operator_response": "bdb-operator-response-v1",
            "event": "bdb-event-v1",
            "session_history": "bdb-session-history-v1",
            "repair_correlation": "bdb-repair-correlation-v1",
            "repair_group": "bdb-repair-group-v1",
            "control_center_smoke": "bdb-control-center-smoke-v1",
            "release_manifest": "bdb-release-manifest-v1",
        },
    }
