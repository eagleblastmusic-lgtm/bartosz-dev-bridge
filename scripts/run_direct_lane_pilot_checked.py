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
_ORIGINAL_SERVICE_JSON = pilot.service_json


def _canonical_content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": pilot.sha256_value(content),
    }


def _checked_initialize_fixture(root: Path) -> tuple[Path, str, bytes, bytes]:
    fixture = root / "fixture"
    fixture.mkdir()
    pilot.git(fixture, "init")
    pilot.git(fixture, "config", "core.autocrlf", "false")
    pilot.git(fixture, "config", "user.name", "BDB Direct Pilot")
    pilot.git(fixture, "config", "user.email", "direct-pilot@example.invalid")
    (fixture / "src").mkdir()
    (fixture / "tests").mkdir()
    before = (
        b"def clamp_percent(value: int) -> int:\n"
        b"    return value\n"
    )
    after = (
        b"def clamp_percent(value: int) -> int:\n"
        b"    return max(0, min(value, 100))\n"
    )
    (fixture / "src" / "clamp.py").write_bytes(before)
    (fixture / "tests" / "test_clamp.py").write_text(
        "from src.clamp import clamp_percent\n\n"
        "def test_clamp_percent() -> None:\n"
        "    assert clamp_percent(-1) == 0\n"
        "    assert clamp_percent(50) == 50\n"
        "    assert clamp_percent(120) == 100\n",
        encoding="utf-8",
        newline="\n",
    )
    pilot.git(fixture, "add", "--", "src/clamp.py", "tests/test_clamp.py")
    pilot.git(fixture, "commit", "-m", "initialize direct pilot fixture")
    status = pilot.git(fixture, "status", "--porcelain=v1")
    if status:
        raise RuntimeError(f"Direct pilot fixture is not clean after initialization: {status}")
    return fixture, pilot.git(fixture, "rev-parse", "HEAD"), before, after


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
    except (RuntimeError, json.JSONDecodeError):
        stdout = completed.stdout
        if isinstance(stdout, bytes):
            text = stdout.decode("utf-8", errors="strict").strip()
        else:
            text = str(stdout).strip()
        if text in _STOP_MESSAGES:
            return {"message": text}
        raise


def _checked_service_json(
    python_executable: str,
    repo_root: Path,
    bridge_config: Path,
    *command: str,
) -> dict[str, Any]:
    response = _ORIGINAL_SERVICE_JSON(
        python_executable,
        repo_root,
        bridge_config,
        *command,
    )
    if command[:2] == ("edit", "status") and "workspace_path" not in response:
        session_id = response.get("session_id")
        if isinstance(session_id, str) and session_id:
            config = json.loads(Path(bridge_config).read_text(encoding="utf-8"))
            worktree_root = Path(str(config["worktree_root"]))
            response = {**response, "workspace_path": str(worktree_root / session_id)}
    return response


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
    pilot.service_json = _checked_service_json
    pilot.wait_until = _checked_wait_until
    pilot.initialize_fixture = _checked_initialize_fixture
    pilot.content_fields = _canonical_content_fields
    code = pilot.main()
    if code == 0:
        _validate_report(_pilot_root())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
