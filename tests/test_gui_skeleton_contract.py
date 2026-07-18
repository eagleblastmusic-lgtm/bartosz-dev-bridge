from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bdb_gui"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def python_sources() -> list[Path]:
    return sorted(GUI.rglob("*.py"))


def attribute_calls(tree: ast.AST) -> set[str]:
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        calls.add(node.func.attr)
    return calls


def test_p06_production_package_and_entrypoint_exist() -> None:
    expected = (
        GUI / "__init__.py",
        GUI / "state.py",
        GUI / "bootstrap.py",
        GUI / "workers.py",
        GUI / "main_window.py",
        GUI / "app.py",
        ROOT / "docs" / "BDB_CONTROL_CENTER_SKELETON.md",
        ROOT / "docs" / "adr" / "0007-read-only-asynchronous-gui-bootstrap.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing P06 artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0

    pyproject = read(ROOT / "pyproject.toml")
    assert 'bdb-control-center = "bdb_gui.app:main"' in pyproject
    assert 'gui = ["PySide6-Essentials>=6.10,<6.12"]' in pyproject
    assert 'include = ["bdb_bridge*", "bdb_operator*", "bdb_gui*", "bdb_poc*"]' in pyproject
    assert "dependencies = []" in pyproject


def test_gui_depends_only_on_public_operator_boundary() -> None:
    for path in python_sources():
        tree = ast.parse(read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(not name.startswith("bdb_bridge") for name in names), (
                f"GUI imports BDB Core directly in {path.relative_to(ROOT)}: {names}"
            )

    bootstrap = read(GUI / "bootstrap.py")
    assert "from bdb_operator import OperatorApi, OperatorResponse" in bootstrap


def test_gui_has_no_process_network_or_git_execution_surface() -> None:
    forbidden_import_roots = {
        "subprocess",
        "socket",
        "socketserver",
        "http",
        "urllib",
        "requests",
        "aiohttp",
        "websockets",
        "fastapi",
        "flask",
    }
    for path in python_sources():
        tree = ast.parse(read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".", 1)[0]]
            else:
                continue
            assert forbidden_import_roots.isdisjoint(names), (
                f"Forbidden execution/network import in {path.relative_to(ROOT)}: {names}"
            )

    combined = "\n".join(read(path) for path in python_sources()).lower()
    for token in ("powershell.exe", "git.exe", "shell=true", "listen(", "bind("):
        assert token not in combined


def test_bootstrap_calls_only_capabilities_and_list_projects() -> None:
    tree = ast.parse(read(GUI / "bootstrap.py"))
    calls = attribute_calls(tree)
    operator_operations = {
        "capabilities",
        "list_projects",
        "status",
        "events",
        "current_operation",
        "logs",
        "prepare",
        "start",
        "stop",
        "rearm",
    }
    assert calls & operator_operations == {"capabilities", "list_projects"}


def test_window_constructor_does_not_start_bootstrap_or_mutate() -> None:
    tree = ast.parse(read(GUI / "main_window.py"))
    window_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ControlCenterWindow"
    )
    constructor = next(
        node for node in window_class.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    constructor_calls = attribute_calls(constructor)
    assert "start_bootstrap" not in constructor_calls
    assert constructor_calls.isdisjoint({"prepare", "start", "stop", "rearm"})

    all_calls = attribute_calls(window_class)
    assert all_calls.isdisjoint({"prepare", "start", "stop", "rearm"})
    assert "QThreadPool" in read(GUI / "main_window.py")
    assert "BootstrapWorker" in read(GUI / "main_window.py")


def test_p06_window_has_no_process_control_buttons() -> None:
    source = read(GUI / "main_window.py")
    for forbidden_label in (
        'QPushButton("Start")',
        'QPushButton("Stop")',
        'QPushButton("Re-arm")',
        'QPushButton("Uzbrój")',
        'QPushButton("Uruchom")',
        'QPushButton("Zatrzymaj")',
    ):
        assert forbidden_label not in source
    assert 'QPushButton("Odśwież odczyt")' in source
    assert "READ-ONLY STARTUP" in source


def test_gui_contains_no_background_polling_loop() -> None:
    combined = "\n".join(read(path) for path in python_sources())
    assert "QTimer.start" not in combined
    assert "while True" not in combined
    assert "watchdog" not in combined.lower()
    assert "setInterval" not in combined


def test_gui_package_is_valid_python_without_importing_optional_qt() -> None:
    for path in python_sources():
        ast.parse(read(path), filename=str(path))
