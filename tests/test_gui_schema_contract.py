from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> dict[str, object]:
    path = ROOT / "schemas" / name
    assert path.is_file(), f"Missing GUI schema: {name}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_bootstrap_schema_is_closed_and_read_only() -> None:
    schema = load("bdb-gui-bootstrap-v1.schema.json")
    assert schema["$id"] == "bdb-gui-bootstrap-v1"
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}


def test_project_status_schema_is_closed_and_read_only() -> None:
    schema = load("bdb-gui-project-status-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}


def test_control_result_schema_has_closed_action_catalog_and_one_mutation() -> None:
    schema = load("bdb-gui-control-result-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["action"]["enum"] == ["start", "stop", "rearm"]
    assert properties["mutation_operations_invoked"] == {"const": 1}


def test_current_operation_schema_is_closed_and_read_only() -> None:
    schema = load("bdb-gui-current-operation-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}


def test_history_schema_is_closed_bounded_and_read_only() -> None:
    schema = load("bdb-gui-history-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    assert properties["events"]["maxItems"] == 500


def test_diagnostics_schema_is_bounded_sanitized_and_read_only() -> None:
    schema = load("bdb-gui-diagnostics-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["schema"] == {"const": "bdb-gui-diagnostics-v1"}
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    assert properties["redaction_version"] == {"const": "bdb-redaction-v1"}
    assert properties["sections"]["maxItems"] == 4


def test_diagnostics_export_schema_requires_zip_receipt_fields() -> None:
    schema = load("bdb-gui-diagnostics-export-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["schema"] == {"const": "bdb-gui-diagnostics-export-v1"}
    assert properties["size_bytes"]["minimum"] == 1
    assert properties["sha256"]["pattern"] == "^sha256:[0-9a-f]{64}$"
    assert properties["entries"]["uniqueItems"] is True


def test_prepare_plan_schema_matches_closed_operator_contract() -> None:
    schema = load("bdb-gui-prepare-plan-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["alias"]["pattern"] == "^[a-z][a-z0-9-]{0,31}$"
    assert properties["allowed_paths"]["maxItems"] == 100
    assert properties["test_timeout_seconds"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 3600,
    }
    for unsupported in (
        "native_config",
        "max_patch_bytes",
        "max_changed_files",
        "auto_send_max_bytes",
        "worker_timeout_seconds",
    ):
        assert unsupported not in properties
    assert properties["requires_confirmation"] == {"const": True}
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}


def test_prepare_result_schema_requires_one_explicit_mutation() -> None:
    schema = load("bdb-gui-prepare-result-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["plan"] == {"$ref": "bdb-gui-prepare-plan-v1"}
    assert properties["mutation_operations_invoked"] == {"const": 1}


def test_control_center_smoke_preserves_0_3_0_zero_mutation_gate() -> None:
    schema = load("bdb-control-center-smoke-v1.schema.json")
    properties = schema["properties"]
    assert "application_version" in schema["required"]
    assert properties["application_version"]["pattern"] == (
        "^[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$"
    )
    assert properties["read_only_startup"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    assert properties["confirmation_required"] == {"const": True}
    assert properties["projects_wizard_present"] == {"const": True}
    assert properties["prepare_plan_required"] == {"const": True}
    assert properties["prepare_confirmation_required"] == {"const": True}
    assert properties["current_operation_read_only"] == {"const": True}
    assert properties["history_read_only"] == {"const": True}
    assert properties["session_history_read_only"] == {"const": True}
    assert properties["session_result_open_explicit"] == {"const": True}
    assert properties["session_receipt_open_explicit"] == {"const": True}
    assert properties["session_folder_open_explicit"] == {"const": True}
    assert properties["session_repair_relationships_inferred"] == {"const": False}
    assert properties["diagnostics_collect_explicit"] == {"const": True}
    assert properties["diagnostics_export_explicit"] == {"const": True}
