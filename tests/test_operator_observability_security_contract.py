from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY = ROOT / "bdb_operator" / "observability.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_journal_projection_is_explicitly_read_only() -> None:
    source = read(OBSERVABILITY)
    assert 'as_uri() + "?mode=ro"' in source
    assert 'connection.execute("PRAGMA query_only = ON")' in source
    assert "Journal.open" not in source
    assert "apply_migrations" not in source


def test_observability_sql_contains_no_mutation_statements() -> None:
    source = read(OBSERVABILITY).upper()
    for statement in (
        "INSERT INTO",
        "UPDATE COMMANDS",
        "UPDATE SESSIONS",
        "UPDATE EVENTS",
        "DELETE FROM",
        "CREATE TABLE",
        "ALTER TABLE",
        "DROP TABLE",
        "BEGIN IMMEDIATE",
    ):
        assert statement not in source


def test_observability_has_hard_payload_and_log_limits() -> None:
    source = read(OBSERVABILITY)
    assert "MAX_EVENT_LIMIT = 500" in source
    assert "MAX_EVENT_PAYLOAD_CHARS = 16_384" in source
    assert "MAX_LOG_BYTES = 65_536" in source
    assert "MAX_LOG_LINES = 500" in source


def test_observability_does_not_start_background_monitoring() -> None:
    source = read(OBSERVABILITY).lower()
    forbidden = (
        "threading",
        "multiprocessing",
        "watchdog",
        "filewatcher",
        "while true",
        "asyncio.create_task",
        "socket",
        "http.server",
        "websocket",
    )
    for token in forbidden:
        assert token not in source


def test_projection_schemas_are_versioned_and_closed() -> None:
    for name, schema_id in (
        ("bdb-event-v1.schema.json", "bdb-event-v1"),
        ("bdb-current-operation-v1.schema.json", "bdb-current-operation-v1"),
        ("bdb-log-snapshot-v1.schema.json", "bdb-log-snapshot-v1"),
    ):
        source = read(ROOT / "schemas" / name)
        assert f'"$id": "{schema_id}"' in source
        assert '"additionalProperties": false' in source
