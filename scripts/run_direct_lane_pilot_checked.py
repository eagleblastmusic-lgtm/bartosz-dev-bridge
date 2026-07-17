from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_direct_lane_pilot as pilot


_NATIVE_MODULE = "bdb_bridge.native_host"
_NATIVE_SHIM = (
    "import sys; "
    "from bdb_bridge.native_host import main; "
    "sys.argv = ['bdb-native-host', *sys.argv[1:]]; "
    "main()"
)
_STOP_MESSAGES = frozenset(
    {
        "Graceful stop request sent successfully.",
        "Service is already OFFLINE.",
        "Service status is STALE. Cannot stop gracefully. Please restart the service.",
        "Service stop is already in progress.",
    }
)
_ORIGINAL_RUN = pilot.run
_ORIGINAL_LOAD_JSON_OUTPUT = pilot.load_json_output
_ORIGINAL_WAIT_UNTIL = pilot.wait_until
_ORIGINAL_INITIALIZE_FIXTURE = pilot.initialize_fixture


def _canonical_content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": pilot.sha256_value(content),
    }


def _checked_initialize_fixture(root: Path) -> tuple[Path, str, bytes, bytes]:
    fixture, base_sha, before, after = _ORIGINAL_INITIALIZE_FIXTURE(root)
    pilot.git(fixture, "config", "core.autocrlf", "false")
    return fixture, base_sha, before, after


def _checked_run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    input_bytes: bytes | None = None,
):
    argv = list(args)
    if len(argv) >= 3 and argv[1:3] == ["-m", _NATIVE_MODULE]:
        argv = [argv[0], "-c", _NATIVE_SHIM, *argv[3:]]
    return _ORIGINAL_RUN(argv, cwd=cwd, check=check, input_bytes=input_bytes)


def _checked_load_json_output(completed: Any) -> dict[str, Any]:
    try:
        return _ORIGINAL_LOAD_JSON_OUTPUT(completed)
    except RuntimeError:
        stdout = completed.stdout
        if isinstance(stdout, bytes):
            text = stdout.decode("utf-8", errors="strict").strip()
        else:
            text = str(stdout).strip()
        if text in _STOP_MESSAGES:
            return {"message": text}
        raise


def _checked_wait_until(description: str, *args: Any, **kwargs: Any) -> Any:
    if description == "Bridge RUNNING":
        time.sleep(1.0)
    return _ORIGINAL_WAIT_UNTIL(description, *args, **kwargs)


def _pilot_root() -> Path:
    try:
        index = sys.argv.index("--root")
        value = sys.argv[index + 1]
    except (ValueError, IndexError) as exc:
        raise RuntimeError("Checked Direct Lane pilot requires --root") from exc
    return Path(value).expanduser().resolve(strict=True)


def _validate_report(root: Path) -> None:
    report_path = root / "direct-lane-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("status") != "success":
        raise RuntimeError("Direct Lane pilot report is not successful")
    before = report.get("before_git_restore")
    after = report.get("after_git_restore")
    if not isinstance(before, dict) or before.get("command_state") != "result_staged":
        raise RuntimeError("Git-offline phase did not finish in exact result_staged state")
    if not isinstance(after, dict) or after.get("command_state") != "result_published":
        raise RuntimeError("Git-restored phase did not finish in exact result_published state")
    if report.get("local_result_before_git_restore") is not True:
        raise RuntimeError("Local result was not proven before Git restoration")
    if report.get("git_fallback_published_without_reexecution") is not True:
        raise RuntimeError("Git fallback publication proof is missing")
    report["checked_runner_validated"] = True
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    pilot.run = _checked_run
    pilot.load_json_output = _checked_load_json_output
    pilot.wait_until = _checked_wait_until
    pilot.initialize_fixture = _checked_initialize_fixture
    pilot.content_fields = _canonical_content_fields
    code = pilot.main()
    if code == 0:
        _validate_report(_pilot_root())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
