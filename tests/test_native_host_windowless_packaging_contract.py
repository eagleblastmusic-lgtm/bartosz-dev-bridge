from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    path = ROOT / "scripts" / "check_native_host_windowless.py"
    spec = importlib.util.spec_from_file_location("check_native_host_windowless", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _minimal_pe(subsystem: int) -> bytes:
    data = bytearray(512)
    data[:2] = b"MZ"
    pe_offset = 0x80
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"
    optional_header = pe_offset + 24
    struct.pack_into("<H", data, optional_header, 0x20B)
    struct.pack_into("<H", data, optional_header + 68, subsystem)
    return bytes(data)


def test_windowless_checker_reads_gui_pe_subsystem(tmp_path: Path) -> None:
    checker = _load_checker()
    executable = tmp_path / "host.exe"
    executable.write_bytes(_minimal_pe(2))

    assert checker.read_pe_subsystem(executable) == 2


def test_windowless_checker_rejects_invalid_pe(tmp_path: Path) -> None:
    checker = _load_checker()
    executable = tmp_path / "host.exe"
    executable.write_bytes(b"not-an-executable")

    with pytest.raises(RuntimeError, match="MZ header"):
        checker.read_pe_subsystem(executable)


def test_native_message_frame_round_trip() -> None:
    checker = _load_checker()
    request = {
        "schema": "bdb-native-request-v1",
        "request_id": "contract",
        "action": "status",
    }

    frame = checker.encode_native_message(request)

    assert checker.decode_native_message(frame) == request


def test_windowless_build_installer_and_project_launcher_contracts_are_explicit() -> None:
    build = (ROOT / "scripts" / "Build-BDBNativeHostWindowless.ps1").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "scripts" / "Install-BDBNativeHost.ps1").read_text(
        encoding="utf-8"
    )
    entry = (ROOT / "packaging" / "windows" / "native_host_entry.py").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "bdb_bridge" / "native_host_project_launcher.py").read_text(
        encoding="utf-8"
    )
    workflow = (
        ROOT / ".github" / "workflows" / "native-host-windowless-candidate.yml"
    ).read_text(encoding="utf-8")

    assert "--windowed" in build
    assert "--onedir" in build
    assert "--collect-submodules bdb_bridge" in build
    assert "native_host_entry.py" in build
    assert "RequireWindowless" in installer
    assert "Get-PeSubsystem" in installer
    assert "expected Windows GUI" in installer
    assert "run_project_launcher_host" in entry
    assert "resolve_native_binary_stdio" in entry
    assert '"project_launch_claim"' in launcher
    assert '"project_launch_ack"' in launcher
    assert "Build-BDBNativeHostWindowless.ps1" in workflow
    assert "check_native_host_windowless.py" in workflow
    assert "-RequireWindowless" in workflow
