"""Resolve the one runtime authority shared by Control Center and Native Host.

During the single-root migration the registered vNext Native Messaging route can
remain authoritative in the retired runtime until cutover/retirement finishes.
The Control Center must project that same runtime.  Falling back unconditionally
to the repo-local runtime can otherwise create a split-brain view where Browser
submissions are accepted in one Project Memory while the GUI reads another.

This module is read-only.  It never registers a Native Host, rewrites a config,
or mutates runtime state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from .composition import (
    BROWSER_EXTENSION_ID,
    GENERATION_ID,
    NATIVE_HOST_NAME,
    PROTOCOL_GENERATION,
    default_vnext_runtime_root,
)


NATIVE_CONFIG_SCHEMA = "bdb-vnext-native-host-config-v2"
NATIVE_REGISTRY_SUBKEY = rf"Software\Google\Chrome\NativeMessagingHosts\{NATIVE_HOST_NAME}"


class RuntimeAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise RuntimeAuthorityError(code, message)


def _absolute(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        _fail("runtime_authority_path_invalid", f"{field} must be absolute")
    return path.absolute()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left.absolute()))) == os.path.normcase(
        os.path.normpath(str(right.absolute()))
    )


def _load_json(path: Path, *, field: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        _fail("runtime_authority_record_invalid", f"{field} must be a bounded regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeAuthorityError(
            "runtime_authority_record_invalid",
            f"{field} is not valid JSON",
        ) from exc
    if not isinstance(value, Mapping):
        _fail("runtime_authority_record_invalid", f"{field} must contain an object")
    return dict(value)


def _registered_manifest_path() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NATIVE_REGISTRY_SUBKEY) as key:
            value, _kind = winreg.QueryValueEx(key, None)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeAuthorityError(
            "runtime_authority_registry_unavailable",
            "vNext Native Messaging registration could not be read",
        ) from exc
    if not isinstance(value, str) or not value.strip():
        _fail("runtime_authority_registry_invalid", "vNext Native Messaging registration is invalid")
    text = value.strip().strip('"')
    return _absolute(text, field="native_manifest_path")


def runtime_from_native_manifest(manifest_path: str | Path) -> Path:
    """Validate one registered vNext Native route and return its bound runtime."""

    manifest_path = _absolute(manifest_path, field="native_manifest_path")
    manifest = _load_json(manifest_path, field="native manifest")
    if (
        manifest.get("name") != NATIVE_HOST_NAME
        or manifest.get("type") != "stdio"
        or manifest.get("allowed_origins") != [f"chrome-extension://{BROWSER_EXTENSION_ID}/"]
    ):
        _fail("runtime_authority_manifest_identity_mismatch", "registered Native manifest identity differs")

    executable_value = manifest.get("path")
    if not isinstance(executable_value, str) or not executable_value.strip():
        _fail("runtime_authority_manifest_invalid", "registered Native manifest has no executable path")
    executable = _absolute(executable_value, field="native_executable")
    if executable.is_symlink() or not executable.is_file():
        _fail("runtime_authority_native_missing", "registered vNext Native executable is missing")

    native_dir = executable.parent
    if native_dir.name.casefold() != "native-host" or native_dir.parent.name.casefold() != "clients":
        _fail(
            "runtime_authority_layout_mismatch",
            "registered vNext Native executable is not staged under runtime/clients/native-host",
        )
    runtime = native_dir.parent.parent.absolute()
    expected_manifest = native_dir / f"{NATIVE_HOST_NAME}.json"
    if not _same_path(manifest_path, expected_manifest):
        _fail(
            "runtime_authority_manifest_path_mismatch",
            "registered Native manifest is not the manifest structurally bound to its runtime",
        )

    config_path = runtime / "config" / "native-host.json"
    config = _load_json(config_path, field="native config")
    if (
        config.get("schema") != NATIVE_CONFIG_SCHEMA
        or config.get("generation_id") != GENERATION_ID
        or config.get("protocol_generation") != PROTOCOL_GENERATION
        or config.get("native_host_name") != NATIVE_HOST_NAME
        or config.get("browser_extension_id") != BROWSER_EXTENSION_ID
    ):
        _fail("runtime_authority_config_identity_mismatch", "registered Native config identity differs")
    configured_value = config.get("runtime_root")
    if not isinstance(configured_value, str) or not configured_value.strip():
        _fail("runtime_authority_config_invalid", "registered Native config has no runtime_root")
    configured_runtime = _absolute(configured_value, field="native_config.runtime_root")
    if not _same_path(configured_runtime, runtime):
        _fail(
            "runtime_authority_config_root_mismatch",
            "registered Native config points at a different runtime than its staged executable",
        )
    return runtime


@dataclass(frozen=True)
class RuntimeAuthorityResolution:
    runtime_root: Path
    source: str
    native_manifest_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_root": str(self.runtime_root),
            "source": self.source,
            "native_manifest_path": None if self.native_manifest_path is None else str(self.native_manifest_path),
        }


def resolve_control_center_runtime(
    *,
    fallback_runtime_root: str | Path | None = None,
    native_manifest_path: str | Path | None = None,
) -> RuntimeAuthorityResolution:
    """Resolve the runtime to project in Control Center without mutating anything.

    An explicitly supplied ``native_manifest_path`` is primarily a bounded test
    and diagnostic seam.  In normal Windows operation the active Chrome Native
    Messaging registration is the authority while it exists.  If no vNext
    Native route is registered, the repo-local default remains the read-only
    fallback.
    """

    fallback = _absolute(
        fallback_runtime_root if fallback_runtime_root is not None else default_vnext_runtime_root(),
        field="fallback_runtime_root",
    )
    manifest = _absolute(native_manifest_path, field="native_manifest_path") if native_manifest_path is not None else _registered_manifest_path()
    if manifest is None:
        return RuntimeAuthorityResolution(runtime_root=fallback, source="repo_default")
    runtime = runtime_from_native_manifest(manifest)
    return RuntimeAuthorityResolution(runtime_root=runtime, source="registered_native_route", native_manifest_path=manifest)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve the canonical BDB vNext runtime used by the active Native route")
    parser.add_argument("--fallback-runtime-root", default=None)
    parser.add_argument("--native-manifest", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        resolution = resolve_control_center_runtime(
            fallback_runtime_root=args.fallback_runtime_root,
            native_manifest_path=args.native_manifest,
        )
    except RuntimeAuthorityError as exc:
        print(f"BDB_RUNTIME_AUTHORITY_ERROR:{exc.code}:{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(resolution.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(str(resolution.runtime_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "NATIVE_CONFIG_SCHEMA",
    "NATIVE_REGISTRY_SUBKEY",
    "RuntimeAuthorityError",
    "RuntimeAuthorityResolution",
    "resolve_control_center_runtime",
    "runtime_from_native_manifest",
]
