"""Operator CLI for the bounded post-ACTIVE maintenance boundary."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from bdb_shared.evidence import canonical_json_bytes
from bdb_vnext.bootstrap import BootstrapError
from bdb_vnext.m11c_post_active_maintenance import (
    M11cMaintenanceError,
    apply_post_active_maintenance,
    prepare_post_active_maintenance,
    query_post_active_maintenance,
)
from bdb_vnext.windows_elevation import (
    ElevationError,
    is_elevated,
    run_elevated_python_module,
)


_ELEVATED_RESULT_SCHEMA = "bdb-vnext-elevated-maintenance-result-v1"
_ELEVATION_FLAGS = frozenset({"--elevate-on-lock", "--elevated-child"})


def _add_elevation_flags(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--elevate-on-lock",
        action="store_true",
        help="On Windows, request UAC elevation only when protected Bootstrap authority rejects the lock.",
    )
    command.add_argument("--elevated-child", action="store_true", help=argparse.SUPPRESS)
    command.add_argument("--elevated-result-file", default=None, help=argparse.SUPPRESS)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb-vnext-maintenance")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    for name in (
        "authority-root",
        "candidate-bundle-root",
        "candidate-client-runtime-root",
        "source-head",
        "source-tree",
        "native-artifact-manifest-sha256",
        "maintenance-id",
    ):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument("--candidate-bundle-sha256", required=True)
    prepare.add_argument("--canonical-runtime-root", default=None)
    _add_elevation_flags(prepare)

    query = sub.add_parser("query")
    query.add_argument("--authority-root", required=True)
    query.add_argument("--maintenance-id", required=True)
    _add_elevation_flags(query)

    apply = sub.add_parser("apply")
    apply.add_argument("--authority-root", required=True)
    apply.add_argument("--maintenance-id", required=True)
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--approve", action="store_true")
    _add_elevation_flags(apply)
    return parser


def _execute(args: argparse.Namespace) -> Mapping[str, Any]:
    if args.command == "prepare":
        return prepare_post_active_maintenance(
            authority_root=args.authority_root,
            candidate_bundle_root=args.candidate_bundle_root,
            candidate_bundle_sha256=args.candidate_bundle_sha256,
            candidate_client_runtime_root=args.candidate_client_runtime_root,
            source_head=args.source_head,
            source_tree=args.source_tree,
            native_artifact_manifest_sha256=args.native_artifact_manifest_sha256,
            maintenance_id=args.maintenance_id,
            canonical_runtime_root=args.canonical_runtime_root,
        )
    if args.command == "query":
        return query_post_active_maintenance(
            authority_root=args.authority_root,
            maintenance_id=args.maintenance_id,
        )
    return apply_post_active_maintenance(
        authority_root=args.authority_root,
        maintenance_id=args.maintenance_id,
        expected_plan_sha256=args.plan_sha256,
        operator_approved=args.approve,
    )


def _blocked(exc: BaseException) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "error_code": getattr(exc, "code", "maintenance_failed"),
        "error": str(exc),
    }


def _write_elevated_result(path: str | Path, *, exit_code: int, payload: Mapping[str, Any]) -> None:
    target = Path(path).expanduser().absolute()
    envelope = {
        "schema": _ELEVATED_RESULT_SCHEMA,
        "exit_code": int(exit_code),
        "payload": dict(payload),
    }
    staging = target.with_name(f".{target.name}.partial-{os.getpid()}")
    try:
        staging.write_bytes(canonical_json_bytes(envelope))
        os.replace(staging, target)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass


def _emit(args: argparse.Namespace, payload: Mapping[str, Any], *, exit_code: int) -> int:
    if args.elevated_child and args.elevated_result_file:
        _write_elevated_result(args.elevated_result_file, exit_code=exit_code, payload=payload)
    else:
        print(canonical_json_bytes(dict(payload)).decode("utf-8"))
    return exit_code


def _child_argv(raw: Sequence[str], result_path: Path) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for item in raw:
        if skip_next:
            skip_next = False
            continue
        if item in _ELEVATION_FLAGS:
            continue
        if item == "--elevated-result-file":
            skip_next = True
            continue
        cleaned.append(str(item))
    cleaned.extend(["--elevated-child", "--elevated-result-file", str(result_path)])
    return cleaned


def _read_elevated_result(path: Path) -> tuple[int, Mapping[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ElevationError("elevation_result_invalid", "elevated maintenance did not return a valid result") from exc
    if not isinstance(value, dict) or value.get("schema") != _ELEVATED_RESULT_SCHEMA:
        raise ElevationError("elevation_result_invalid", "elevated maintenance result schema differs")
    exit_code = value.get("exit_code")
    payload = value.get("payload")
    if not isinstance(exit_code, int) or exit_code not in {0, 2} or not isinstance(payload, dict):
        raise ElevationError("elevation_result_invalid", "elevated maintenance result fields differ")
    return exit_code, payload


def _retry_elevated(raw: Sequence[str]) -> tuple[int, Mapping[str, Any]]:
    fd, raw_path = tempfile.mkstemp(prefix="bdb-maintenance-elevated-", suffix=".json")
    os.close(fd)
    result_path = Path(raw_path)
    try:
        result_path.unlink(missing_ok=True)
        process_exit = run_elevated_python_module(
            "bdb_vnext.m11c_post_active_maintenance_cli",
            _child_argv(raw, result_path),
            cwd=Path.cwd(),
        )
        exit_code, payload = _read_elevated_result(result_path)
        if process_exit != exit_code:
            raise ElevationError(
                "elevation_exit_mismatch",
                f"elevated process exit {process_exit} differs from result exit {exit_code}",
            )
        return exit_code, payload
    finally:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else list(__import__("sys").argv[1:])
    args = _parser().parse_args(raw)
    try:
        result = _execute(args)
        return _emit(args, result, exit_code=0)
    except (M11cMaintenanceError, BootstrapError, OSError, ValueError) as exc:
        if (
            getattr(exc, "code", None) == "authority_lock_failed"
            and args.elevate_on_lock
            and not args.elevated_child
            and os.name == "nt"
            and not is_elevated()
        ):
            try:
                exit_code, payload = _retry_elevated(raw)
                print(canonical_json_bytes(dict(payload)).decode("utf-8"))
                return exit_code
            except ElevationError as elevation_exc:
                print(canonical_json_bytes(_blocked(elevation_exc)).decode("utf-8"))
                return 2
        return _emit(args, _blocked(exc), exit_code=2)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
