from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_vnext.composition import BROWSER_EXTENSION_ID, GENERATION_ID, NATIVE_HOST_NAME, PROTOCOL_GENERATION
from bdb_vnext.runtime_authority import RuntimeAuthorityError, resolve_control_center_runtime, runtime_from_native_manifest


def _stage_route(tmp_path: Path, *, configured_runtime: Path | None = None, extension_id: str = BROWSER_EXTENSION_ID) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    native_dir = runtime / "clients" / "native-host"
    config_dir = runtime / "config"
    native_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)

    executable = native_dir / "BDB-vNext-NativeHost.exe"
    executable.write_bytes(b"native")
    manifest = native_dir / f"{NATIVE_HOST_NAME}.json"
    manifest.write_text(
        json.dumps(
            {
                "name": NATIVE_HOST_NAME,
                "description": "test",
                "path": str(executable),
                "type": "stdio",
                "allowed_origins": [f"chrome-extension://{BROWSER_EXTENSION_ID}/"],
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "native-host.json").write_text(
        json.dumps(
            {
                "schema": "bdb-vnext-native-host-config-v2",
                "generation_id": GENERATION_ID,
                "protocol_generation": PROTOCOL_GENERATION,
                "native_host_name": NATIVE_HOST_NAME,
                "browser_extension_id": extension_id,
                "runtime_root": str(configured_runtime or runtime),
                "legacy_runtime_root": str(tmp_path / "legacy"),
                "bootstrap_authority_root": str(tmp_path / "bootstrap"),
            }
        ),
        encoding="utf-8",
    )
    return runtime, manifest


def test_falls_back_to_repo_runtime_without_registered_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = tmp_path / "repo-runtime"
    fallback.mkdir()
    monkeypatch.setattr("bdb_vnext.runtime_authority._registered_manifest_path", lambda: None)

    result = resolve_control_center_runtime(fallback_runtime_root=fallback)

    assert result.runtime_root == fallback.absolute()
    assert result.source == "repo_default"
    assert result.native_manifest_path is None


def test_registered_native_route_is_control_center_authority(tmp_path: Path) -> None:
    fallback = tmp_path / "repo-runtime"
    fallback.mkdir()
    runtime, manifest = _stage_route(tmp_path)

    result = resolve_control_center_runtime(
        fallback_runtime_root=fallback,
        native_manifest_path=manifest,
    )

    assert result.runtime_root == runtime.absolute()
    assert result.source == "registered_native_route"
    assert result.native_manifest_path == manifest.absolute()
    assert runtime_from_native_manifest(manifest) == runtime.absolute()


def test_config_runtime_mismatch_fails_closed(tmp_path: Path) -> None:
    other = tmp_path / "other-runtime"
    other.mkdir()
    _runtime, manifest = _stage_route(tmp_path, configured_runtime=other)

    with pytest.raises(RuntimeAuthorityError) as raised:
        runtime_from_native_manifest(manifest)

    assert raised.value.code == "runtime_authority_config_root_mismatch"


def test_native_config_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    _runtime, manifest = _stage_route(tmp_path, extension_id="a" * 32)

    with pytest.raises(RuntimeAuthorityError) as raised:
        runtime_from_native_manifest(manifest)

    assert raised.value.code == "runtime_authority_config_identity_mismatch"


def test_manifest_must_be_structurally_bound_to_staged_runtime(tmp_path: Path) -> None:
    _runtime, manifest = _stage_route(tmp_path)
    detached = tmp_path / "detached.json"
    detached.write_bytes(manifest.read_bytes())

    with pytest.raises(RuntimeAuthorityError) as raised:
        runtime_from_native_manifest(detached)

    assert raised.value.code == "runtime_authority_manifest_path_mismatch"


def test_start_bdb_resolves_runtime_before_launching_gui() -> None:
    script = (Path(__file__).resolve().parents[1] / "Start-BDB.ps1").read_text(encoding="utf-8")

    assert "-m bdb_vnext.runtime_authority" in script
    assert '@("-m", "bdb_gui.app", "--runtime-root", $RuntimeRoot)' in script
    assert "BDB runtime authority resolution failed" in script
