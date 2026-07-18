from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "spikes" / "gui" / "pyside6_probe.py"
DECISION = ROOT / "docs" / "BDB_GUI_TECHNOLOGY_SPIKE.md"
ADR = ROOT / "docs" / "adr" / "0006-pyside6-qt-widgets-for-control-center-mvp.md"
PYPROJECT = ROOT / "pyproject.toml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p05_decision_and_probe_exist() -> None:
    for path in (PROBE, DECISION, ADR, ROOT / "spikes" / "gui" / "README.md"):
        assert path.is_file(), f"Missing P05 artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_probe_is_valid_python_and_remains_separate_from_production_package() -> None:
    ast.parse(read(PROBE))
    assert (ROOT / "bdb_gui").is_dir()
    assert PROBE.parts[-3:-1] == ("spikes", "gui")
    assert not str(PROBE.relative_to(ROOT)).startswith("bdb_gui/")


def test_pyside6_remains_optional_after_p06() -> None:
    pyproject = read(PYPROJECT)
    assert "dependencies = []" in pyproject
    assert 'gui = ["PySide6-Essentials>=6.10,<6.12"]' in pyproject
    assert 'gui-spike = ["PySide6-Essentials>=6.10,<6.12"]' in pyproject
    assert 'include = ["bdb_bridge*", "bdb_operator*", "bdb_gui*", "bdb_poc*"]' in pyproject


def test_decision_selects_widgets_and_preserves_fallback() -> None:
    decision = read(DECISION)
    adr = read(ADR)
    assert "PySide6 + Qt Widgets" in decision
    assert "WPF pozostaje kandydatem rezerwowym" in decision
    assert "Qt Quick/QML nie jest częścią pierwszego MVP" in adr
    assert "PySide6 nie jest bazową zależnością" in adr
    assert "bdb_gui -> bdb_operator" in adr


def test_probe_has_no_workspace_or_mutation_surface() -> None:
    source = read(PROBE)
    forbidden = (
        "workspace_root",
        "workspace-loop-state",
        ".start(",
        ".stop(",
        ".rearm(",
        ".prepare(",
        "subprocess",
        "powershell",
        "shell=True",
        "socket",
        "http.server",
        "websocket",
    )
    lowered = source.lower()
    for token in forbidden:
        assert token.lower() not in lowered
    assert "OperatorApi().capabilities()" in source
    assert '"mutation_operations_invoked": 0' in source


def test_probe_exposes_required_desktop_capability_checks() -> None:
    source = read(PROBE)
    for marker in (
        "QApplication",
        "QMainWindow",
        "QSystemTrayIcon",
        "devicePixelRatio",
        'QT_QPA_PLATFORM", "offscreen"',
        "application.exec()",
    ):
        assert marker in source
