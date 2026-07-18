from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPERATOR = ROOT / "bdb_operator"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_subprocess_adapter_is_explicitly_shell_free() -> None:
    runner = read(OPERATOR / "runner.py")
    assert "shell=False" in runner
    assert "shell=True" not in runner


def test_operator_package_has_no_network_listener() -> None:
    source = "\n".join(read(path) for path in OPERATOR.rglob("*.py"))
    forbidden = (
        "import socket",
        "from socket",
        "http.server",
        "socketserver",
        "websockets",
        "aiohttp",
        "fastapi",
        "flask",
        "uvicorn",
    )
    lowered = source.lower()
    for token in forbidden:
        assert token.lower() not in lowered


def test_operator_exposes_only_closed_public_operations() -> None:
    api = read(OPERATOR / "api.py")
    for operation in (
        "def capabilities(",
        "def list_projects(",
        "def status(",
        "def start(",
        "def stop(",
        "def rearm(",
        "def prepare(",
    ):
        assert operation in api
    assert "def execute_shell(" not in api
    assert "def run_command(" not in api


def test_operator_response_schema_is_versioned_and_closed() -> None:
    schema = read(ROOT / "schemas" / "bdb-operator-response-v1.schema.json")
    assert '"$id": "bdb-operator-response-v1"' in schema
    assert '"additionalProperties": false' in schema
