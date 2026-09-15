from __future__ import annotations

from pathlib import Path


def test_gui_entrypoint_uses_direct_resilient_project_center_composition():
    app_source = Path("bdb_gui/app.py").read_text(encoding="utf-8")
    composition_source = Path("bdb_gui/resilient_project_center.py").read_text(encoding="utf-8")

    assert "ResilientProjectCenterWindow as VNextControlCenterWindow" in app_source
    assert "_project_center.ProjectWorkflow = ResilientProjectWorkflow" not in app_source
    assert "workflow = ResilientProjectWorkflow" in composition_source
    assert "super().__init__(runtime_root=runtime_root, catalog=catalog, workflow=workflow" in composition_source
