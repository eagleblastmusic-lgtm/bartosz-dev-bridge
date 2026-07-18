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


def test_control_center_smoke_schema_preserves_zero_mutation_gate() -> None:
    schema = load("bdb-control-center-smoke-v1.schema.json")

    assert schema["$id"] == "bdb-control-center-smoke-v1"
    properties = schema["properties"]
    assert properties["read_only_startup"] == {"const": True}
    assert properties["mutation_operations_invoked"] == {"const": 0}
    assert properties["status"]["enum"] == ["success", "failed"]
