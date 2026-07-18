from __future__ import annotations

import time
from pathlib import Path

import pytest

from bdb_gui.diagnostics import (
    DiagnosticsExportResult,
    DiagnosticsSection,
    DiagnosticsSnapshot,
)
from bdb_gui.state import BootstrapSnapshot, GuiProject


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from bdb_gui.main_window import ControlCenterWindow  # noqa: E402


class UnusedBootstrapService:
    def load(self, root: str):  # pragma: no cover
        raise AssertionError("constructor must not call bootstrap")


class UnusedOperationsService:
    def read_status(self, root: str):  # pragma: no cover
        raise AssertionError("diagnostics flow must not read status")

    def execute(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("diagnostics flow must not mutate")


class UnusedCurrentOperationService:
    def read(self, root: str):  # pragma: no cover
        raise AssertionError("diagnostics flow must not read current operation")


class UnusedHistoryService:
    def read(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("diagnostics flow must not read history")


class FakeDiagnosticsService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def collect(self, workspace_root: str) -> DiagnosticsSnapshot:
        self.calls.append(workspace_root)
        return DiagnosticsSnapshot(
            workspace_root=workspace_root,
            generated_at="2026-07-18T22:00:00Z",
            sections=(
                DiagnosticsSection(
                    name="capabilities",
                    ok=True,
                    operation_id="cap-op",
                    project_alias="alpha",
                    data={"transport": "in_process"},
                ),
            ),
            versions={"python": "3.12.0"},
        )


class FakeDiagnosticsExporter:
    def __init__(self) -> None:
        self.calls: list[tuple[DiagnosticsSnapshot, str, bool]] = []

    def export(
        self,
        snapshot: DiagnosticsSnapshot,
        output_path: str,
        *,
        overwrite: bool,
    ) -> DiagnosticsExportResult:
        self.calls.append((snapshot, output_path, overwrite))
        path = Path(output_path)
        path.write_bytes(b"fake-zip")
        return DiagnosticsExportResult(
            output_path=str(path.resolve()),
            size_bytes=path.stat().st_size,
            sha256="sha256:" + "a" * 64,
            entries=("diagnostics.json", "manifest.json"),
            generated_at="2026-07-18T22:01:00Z",
        )


def application() -> QApplication:
    return QApplication.instance() or QApplication(["test-diagnostics-window"])


def wait_until(predicate, timeout: float = 5.0) -> None:
    app = application()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def project_snapshot(tmp_path: Path) -> BootstrapSnapshot:
    return BootstrapSnapshot.success(
        workspaces_root=str(tmp_path),
        projects=(
            GuiProject(
                alias="alpha",
                workspace_root=str(tmp_path / "alpha"),
                source_repo=str(tmp_path / "source"),
                source_branch="main",
                configured_status="prepared",
            ),
        ),
        operator_schema="bdb-operator-response-v1",
        operator_transport="in_process",
        network_listener=False,
        journal_access="read_only",
    )


def make_window(
    tmp_path: Path,
    diagnostics: FakeDiagnosticsService,
    exporter: FakeDiagnosticsExporter,
    *,
    output_path: str | None,
    overwrite: bool = False,
) -> ControlCenterWindow:
    window = ControlCenterWindow(
        bootstrap_service=UnusedBootstrapService(),  # type: ignore[arg-type]
        operations_service=UnusedOperationsService(),  # type: ignore[arg-type]
        current_operation_service=UnusedCurrentOperationService(),  # type: ignore[arg-type]
        history_service=UnusedHistoryService(),  # type: ignore[arg-type]
        diagnostics_service=diagnostics,  # type: ignore[arg-type]
        diagnostics_exporter=exporter,  # type: ignore[arg-type]
        workspaces_root=str(tmp_path),
        auto_load_status=False,
        confirmation_provider=lambda action, root: False,
        export_path_provider=lambda suggested: (output_path, overwrite),
    )
    window._apply_bootstrap_snapshot(project_snapshot(tmp_path))
    return window


def test_generic_smoke_flow_does_not_collect_or_export(tmp_path: Path) -> None:
    diagnostics = FakeDiagnosticsService()
    exporter = FakeDiagnosticsExporter()
    window = make_window(tmp_path, diagnostics, exporter, output_path=None)
    application().processEvents()

    report = window.smoke_report()

    assert diagnostics.calls == []
    assert exporter.calls == []
    assert report["diagnostics_view_present"] is True
    assert report["diagnostics_collect_explicit"] is True
    assert report["diagnostics_export_explicit"] is True
    assert report["diagnostics_snapshot_loaded"] is False
    assert report["diagnostics_export_completed"] is False
    assert report["mutation_operations_invoked"] == 0
    window.close()


def test_explicit_collect_runs_once_and_remains_read_only(tmp_path: Path) -> None:
    diagnostics = FakeDiagnosticsService()
    exporter = FakeDiagnosticsExporter()
    window = make_window(tmp_path, diagnostics, exporter, output_path=None)

    window._start_diagnostics_collect()
    wait_until(lambda: window.last_diagnostics is not None)

    workspace = str(tmp_path / "alpha")
    assert diagnostics.calls == [workspace]
    assert exporter.calls == []
    assert window.last_diagnostics is not None
    assert window.last_diagnostics.read_only is True
    assert window.last_diagnostics.mutation_operations_invoked == 0
    assert window.diagnostics_view.export_button.isEnabled() is True
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    window.close()


def test_cancelled_export_provider_does_not_write(tmp_path: Path) -> None:
    diagnostics = FakeDiagnosticsService()
    exporter = FakeDiagnosticsExporter()
    window = make_window(tmp_path, diagnostics, exporter, output_path=None)
    window._start_diagnostics_collect()
    wait_until(lambda: window.last_diagnostics is not None)

    window._request_diagnostics_export()
    application().processEvents()

    assert exporter.calls == []
    assert window.last_diagnostics_export is None
    assert "anulowany" in window.status_line.text()
    window.close()


def test_explicit_export_runs_once_after_snapshot(tmp_path: Path) -> None:
    diagnostics = FakeDiagnosticsService()
    exporter = FakeDiagnosticsExporter()
    target = tmp_path / "diagnostics.zip"
    window = make_window(
        tmp_path,
        diagnostics,
        exporter,
        output_path=str(target),
        overwrite=True,
    )
    window._start_diagnostics_collect()
    wait_until(lambda: window.last_diagnostics is not None)

    window._request_diagnostics_export()
    wait_until(lambda: window.last_diagnostics_export is not None)

    assert len(exporter.calls) == 1
    snapshot, output_path, overwrite = exporter.calls[0]
    assert snapshot is window.last_diagnostics
    assert output_path == str(target)
    assert overwrite is True
    assert target.read_bytes() == b"fake-zip"
    assert window.last_diagnostics_export is not None
    assert window.last_diagnostics_export.output_path == str(target.resolve())
    assert window.smoke_report()["diagnostics_export_completed"] is True
    assert window.smoke_report()["mutation_operations_invoked"] == 0
    window.close()


def test_export_before_collection_is_ignored(tmp_path: Path) -> None:
    diagnostics = FakeDiagnosticsService()
    exporter = FakeDiagnosticsExporter()
    target = tmp_path / "diagnostics.zip"
    window = make_window(tmp_path, diagnostics, exporter, output_path=str(target))

    window._request_diagnostics_export()
    application().processEvents()

    assert diagnostics.calls == []
    assert exporter.calls == []
    assert not target.exists()
    window.close()
