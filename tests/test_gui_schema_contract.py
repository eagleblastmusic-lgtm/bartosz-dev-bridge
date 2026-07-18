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
    section = properties["sections"]["items"]
    assert section["additionalProperties"] is False
    assert section["properties"]["name"]["enum"] == [
        "capabilities", "status", "current_operation", "logs", "collection"
    ]


def test_diagnostics_export_schema_requires_zip_receipt_fields() -> None:
    schema = load("bdb-gui-diagnostics-export-v1.schema.json")
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert properties["schema"] == {"const": "bdb-gui-diagnostics-export-v1"}
    assert properties["size_bytes"]["minimum"] == 1
    assert properties["sha256"]["pattern"] == "^sha256:[0-9a-f]{64}$"
    assert properties["entries"]["uniqueItems"] is True


def test_control_center_smoke_preserves_zero_mutation_startup_gate() -> None:
    schema = load("bdb-control-center-smoke-v1.schema.json")
    properties = schema["properties"]
    assert properties["read_only_startup"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    assert properties["confirmation_required"] == {"const": True}
    assert properties["current_operation_read_only"] == {"const": True}
    assert properties["history_read_only"] == {"const": True}
    assert properties["diagnostics_view_present"] == {"type": "boolean"}
    assert properties["diagnostics_collect_explicit"] == {"const": True}
    assert properties["diagnostics_export_explicit"] == {"const": True}
