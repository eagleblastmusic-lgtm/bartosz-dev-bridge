from __future__ import annotations

import json
from pathlib import Path

from bdb_gui.runtime_paths import default_python_executable


def test_default_python_accepts_running_python(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    python.write_bytes(b"")

    assert default_python_executable(
        current_executable=python,
        environment={},
        module_file=tmp_path / "repo" / "bdb_gui" / "runtime_paths.py",
    ) == str(python.resolve())


def test_default_python_uses_explicit_override(tmp_path: Path) -> None:
    override = tmp_path / "custom-python.exe"
    override.write_bytes(b"")

    assert default_python_executable(
        current_executable=tmp_path / "BDB-Control-Center.exe",
        environment={"BDB_PYTHON_EXECUTABLE": str(override)},
        module_file=tmp_path / "repo" / "bdb_gui" / "runtime_paths.py",
    ) == str(override.resolve())


def test_packaged_control_center_uses_native_host_sibling_python(
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    install = local_app_data / "BartoszDevBridge"
    install.mkdir(parents=True)
    scripts = tmp_path / "repo" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    host = scripts / "bdb-native-host.exe"
    python = scripts / "python.exe"
    host.write_bytes(b"")
    python.write_bytes(b"")
    (install / "com.bartosz.dev_bridge.json").write_text(
        json.dumps({"path": str(host)}),
        encoding="utf-8",
    )
    packaged = tmp_path / "BDB-Control-Center.exe"
    packaged.write_bytes(b"")

    assert default_python_executable(
        current_executable=packaged,
        environment={"LOCALAPPDATA": str(local_app_data)},
        module_file=tmp_path / "bundle" / "bdb_gui" / "runtime_paths.py",
    ) == str(python.resolve())


def test_packaged_control_center_never_returns_its_own_executable(
    tmp_path: Path,
) -> None:
    packaged = tmp_path / "BDB-Control-Center.exe"
    packaged.write_bytes(b"")

    assert (
        default_python_executable(
            current_executable=packaged,
            environment={},
            module_file=tmp_path / "bundle" / "bdb_gui" / "runtime_paths.py",
        )
        == ""
    )
