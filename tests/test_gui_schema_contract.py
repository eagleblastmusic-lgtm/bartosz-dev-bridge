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
    project = properties["projects"]["items"]
    assert project["additionalProperties"] is False
    assert project["properties"]["schema"] == {"const": "bdb-gui-project-v1"}


def test_project_status_schema_is_closed_and_read_only() -> None:
    schema = load("bdb-gui-project-status-v1.schema.json")
    assert schema["$id"] == "bdb-gui-project-status-v1"
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["schema"] == {"const": "bdb-gui-project-status-v1"}
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}


def test_control_result_schema_has_closed_action_catalog_and_one_mutation() -> None:
    schema = load("bdb-gui-control-result-v1.schema.json")
    assert schema["$id"] == "bdb-gui-control-result-v1"
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["schema"] == {"const": "bdb-gui-control-result-v1"}
    assert properties["action"]["enum"] == ["start", "stop", "rearm"]
    assert properties["mutation_operations_invoked"] == {"const": 1}


def test_current_operation_schema_is_closed_and_read_only() -> None:
    schema = load("bdb-gui-current-operation-v1.schema.json")
    assert schema["$id"] == "bdb-gui-current-operation-v1"
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["schema"] == {"const": "bdb-gui-current-operation-v1"}
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    operation = properties["operation"]["oneOf"][1]
    assert operation["additionalProperties"] is False
    assert operation["properties"]["schema"] == {"const": "bdb-gui-operation-details-v1"}


def test_history_schema_is_closed_bounded_and_read_only() -> None:
    schema = load("bdb-gui-history-v1.schema.json")
    assert schema["$id"] == "bdb-gui-history-v1"
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["schema"] == {"const": "bdb-gui-history-v1"}
    assert properties["read_only"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    assert properties["events"]["maxItems"] == 500
    event = properties["events"]["items"]
    assert event["additionalProperties"] is False
    assert event["properties"]["schema"] == {"const": "bdb-gui-event-v1"}
    assert properties["cursor"]["additionalProperties"] is False
    assert properties["filters"]["additionalProperties"] is False


def test_control_center_smoke_preserves_zero_mutation_startup_gate() -> None:
    schema = load("bdb-control-center-smoke-v1.schema.json")
    assert schema["$id"] == "bdb-control-center-smoke-v1"
    properties = schema["properties"]
    assert properties["read_only_startup"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    assert properties["status"]["enum"] == ["success", "failed"]
    assert properties["action_controls_present"] == {"type": "boolean"}
    assert properties["confirmation_required"] == {"const": True}
    assert properties["current_operation_view_present"] == {"type": "boolean"}
    assert properties["current_operation_read_only"] == {"const": True}
    assert properties["history_view_present"] == {"type": "boolean"}
    assert properties["history_read_only"] == {"const": True}
    assert properties["history_pagination_present"] == {"type": "boolean"}
