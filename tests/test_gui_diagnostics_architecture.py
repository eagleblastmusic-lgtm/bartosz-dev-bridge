from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bdb_gui"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p10_artifacts_exist() -> None:
    expected = (
        GUI / "diagnostics.py",
        GUI / "diagnostics_tasks.py",
        GUI / "diagnostics_view.py",
        GUI / "style.py",
        ROOT / "schemas" / "bdb-gui-diagnostics-v1.schema.json",
        ROOT / "schemas" / "bdb-gui-diagnostics-export-v1.schema.json",
        ROOT / "docs" / "BDB_CONTROL_CENTER_DIAGNOSTICS.md",
        ROOT / "docs" / "adr" / "0011-explicit-sanitized-diagnostics-export.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing P10 artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_diagnostics_collection_uses_only_bounded_public_operator_reads() -> None:
    source = read(GUI / "diagnostics.py")
    assert "from bdb_operator import OperatorApi, OperatorResponse" in source
    assert "from bdb_operator.observability import MAX_LOG_BYTES" in source
    assert "self._operator.capabilities()" in source
    assert "self._operator.status(root)" in source
    assert "self._operator.current_operation(root)" in source
    assert "self._operator.logs(" in source
    assert "MAX_DIAGNOSTIC_LOG_LINES = 200" in source
    assert "MAX_DIAGNOSTIC_LOG_BYTES = MAX_LOG_BYTES" in source
    for forbidden in (
        ".start(",
        ".stop(",
        ".rearm(",
        ".prepare(",
        ".events(",
        "sqlite3",
        "SELECT ",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    ):
        assert forbidden not in source


def test_export_is_local_atomic_and_explicitly_excludes_source_data() -> None:
    source = read(GUI / "diagnostics.py")
    window = read(GUI / "main_window.py")
    view = read(GUI / "diagnostics_view.py")

    assert "os.replace(temporary, target)" in source
    assert '"contains_journal_database": False' in source
    assert '"contains_repository_files": False' in source
    assert "target.exists() and not overwrite" in source
    assert 'QPushButton("Zbierz diagnostykę")' in view
    assert 'QPushButton("Eksportuj ZIP")' in view
    assert "QFileDialog.getSaveFileName" in window
    assert "export_path_provider" in window
    assert "DiagnosticsCollectWorker" in window
    assert "DiagnosticsExportWorker" in window


def test_secret_redaction_is_applied_before_serialization() -> None:
    source = read(GUI / "diagnostics.py")
    for marker in (
        'REDACTION_VERSION = "bdb-redaction-v1"',
        "_SECRET_KEY",
        "_SECRET_ASSIGNMENT",
        "_BEARER",
        "_sanitize_object",
        '"[REDACTED]"',
    ):
        assert marker in source


def test_main_window_constructor_does_not_collect_or_export() -> None:
    source = read(GUI / "main_window.py")
    tree = ast.parse(source)
    window = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ControlCenterWindow"
    )
    constructor = next(
        node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    calls = {
        node.func.attr
        for node in ast.walk(constructor)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "collect" not in calls
    assert "export" not in calls
    assert "_start_diagnostics_collect" not in calls
    assert "_request_diagnostics_export" not in calls


def test_gui_still_has_no_network_upload_or_remote_telemetry() -> None:
    combined = "\n".join(read(path) for path in GUI.rglob("*.py")).lower()
    for token in (
        "requests.post",
        "requests.put",
        "urllib.request",
        "http.client",
        "websocket",
        "upload_file",
        "google drive",
        "sentry_sdk",
        "opentelemetry",
    ):
        assert token not in combined
