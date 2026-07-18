from __future__ import annotations

import pytest

from bdb_gui.diagnostics import (
    DiagnosticsExportResult,
    DiagnosticsSection,
    DiagnosticsSnapshot,
)


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.diagnostics_view import DiagnosticsWidget  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-diagnostics-view"])


def snapshot(*, complete: bool = True) -> DiagnosticsSnapshot:
    sections = [
        DiagnosticsSection(
            name="capabilities",
            ok=True,
            operation_id="cap-op",
            project_alias="alpha",
            data={"transport": "in_process"},
        )
    ]
    if not complete:
        sections.append(
            DiagnosticsSection(
                name="logs",
                ok=False,
                operation_id="logs-op",
                project_alias="alpha",
                error_code="log_unavailable",
                error_message="Log unavailable",
            )
        )
    return DiagnosticsSnapshot(
        workspace_root="C:/workspaces/alpha",
        generated_at="2026-07-18T22:00:00Z",
        sections=tuple(sections),
        versions={"python": "3.12.0"},
    )


def test_widget_requires_project_and_explicit_collection() -> None:
    app = application()
    widget = DiagnosticsWidget()
    app.processEvents()

    report = widget.smoke_report()
    assert report["diagnostics_view_present"] is True
    assert report["diagnostics_collect_explicit"] is True
    assert report["diagnostics_export_explicit"] is True
    assert report["diagnostics_snapshot_loaded"] is False
    assert report["diagnostics_export_completed"] is False
    assert widget.collect_button.isEnabled() is False
    assert widget.export_button.isEnabled() is False
    widget.close()


def test_collection_signal_and_export_gate_are_separate() -> None:
    app = application()
    widget = DiagnosticsWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    calls: list[str] = []
    widget.collect_requested.connect(lambda: calls.append("collect"))
    widget.export_requested.connect(lambda: calls.append("export"))

    assert widget.collect_button.isEnabled() is True
    assert widget.export_button.isEnabled() is False
    widget.collect_button.click()
    app.processEvents()
    assert calls == ["collect"]

    widget.apply_snapshot(snapshot())
    assert widget.export_button.isEnabled() is True
    widget.export_button.click()
    app.processEvents()
    assert calls == ["collect", "export"]
    widget.close()


def test_complete_and_partial_snapshots_are_visible() -> None:
    app = application()
    widget = DiagnosticsWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")

    widget.apply_snapshot(snapshot())
    app.processEvents()
    assert widget.state_label.text() == "KOMPLETNY"
    assert widget.table.rowCount() == 1
    assert widget.table.item(0, 1).text() == "OK"
    assert '"redaction_version": "bdb-redaction-v1"' in widget.details.toPlainText()

    widget.apply_snapshot(snapshot(complete=False))
    app.processEvents()
    assert widget.state_label.text() == "CZĘŚCIOWY"
    assert widget.table.rowCount() == 2
    assert widget.table.item(1, 1).text() == "BŁĄD"
    assert "log_unavailable" in widget.table.item(1, 3).text()
    widget.close()


def test_busy_state_blocks_collection_and_export() -> None:
    app = application()
    widget = DiagnosticsWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot())
    calls: list[str] = []
    widget.collect_requested.connect(lambda: calls.append("collect"))
    widget.export_requested.connect(lambda: calls.append("export"))

    widget.set_busy(True, "Praca w toku")
    assert widget.collect_button.isEnabled() is False
    assert widget.export_button.isEnabled() is False
    widget.collect_button.click()
    widget.export_button.click()
    app.processEvents()
    assert calls == []
    widget.close()


def test_export_result_and_error_are_reported_separately() -> None:
    app = application()
    widget = DiagnosticsWidget()
    widget.set_project("alpha", "C:/workspaces/alpha")
    widget.apply_snapshot(snapshot())
    result = DiagnosticsExportResult(
        output_path="C:/exports/diagnostics.zip",
        size_bytes=1234,
        sha256="sha256:" + "a" * 64,
        entries=("diagnostics.json", "manifest.json"),
        generated_at="2026-07-18T22:01:00Z",
    )

    widget.apply_export_result(result)
    app.processEvents()
    assert widget.last_export == result
    assert "diagnostics.zip" in widget.feedback_label.text()
    assert widget.smoke_report()["diagnostics_export_completed"] is True

    widget.apply_export_error("export_exists", "File exists")
    assert "export_exists" in widget.feedback_label.text()
    widget.close()
