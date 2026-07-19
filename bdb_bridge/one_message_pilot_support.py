from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .native_messaging import encode_native_message, read_native_message

ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
ALIAS = "calculator2"
_NATIVE_SHIM = (
    "import sys; "
    "from bdb_bridge.native_host import main; "
    "sys.argv = ['bdb-native-host', *sys.argv[1:]]; "
    "main()"
)


def canonical_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_value(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def content_fields(content: bytes) -> dict[str, str]:
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": sha256_value(content),
    }


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        input=input_bytes,
        shell=False,
        capture_output=True,
        text=input_bytes is None,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace") if isinstance(completed.stderr, bytes) else completed.stderr
        stdout = completed.stdout.decode("utf-8", errors="replace") if isinstance(completed.stdout, bytes) else completed.stdout
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return completed


def git(repo: Path, *args: str) -> str:
    return str(run(["git", "-C", str(repo), *args]).stdout).strip()


def load_json_output(completed: subprocess.CompletedProcess[Any]) -> dict[str, Any]:
    stdout = completed.stdout.decode("utf-8", errors="strict") if isinstance(completed.stdout, bytes) else completed.stdout
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Expected JSON object output")
    return value


def wait_until(
    description: str,
    producer: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float,
    process: subprocess.Popen[Any] | None = None,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Bridge exited while waiting for {description}: {process.returncode}")
        try:
            last = producer()
            if predicate(last):
                return last
        except Exception as exc:
            last = exc
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {description}; last={last!r}")


def initialize_control(root: Path) -> tuple[Path, Path]:
    remote = root / "control.git"
    seed = root / "control-seed"
    run(["git", "init", "--bare", str(remote)])
    run(["git", "clone", str(remote), str(seed)])
    git(seed, "config", "user.name", "BDB One Message Pilot")
    git(seed, "config", "user.email", "one-message-pilot@example.invalid")
    (seed / "README.md").write_text("# One-message pilot control\n", encoding="utf-8")
    git(seed, "add", "--", "README.md")
    git(seed, "commit", "-m", "initialize one-message pilot control")
    git(seed, "branch", "-M", "main")
    git(seed, "push", "-u", "origin", "main")
    for branch in ("commands", "results"):
        git(seed, "switch", "-C", branch, "main")
        git(seed, "push", "-u", "origin", branch)
    git(seed, "switch", "main")
    control = root / "bridge-control"
    run(["git", "clone", "--branch", "main", str(remote), str(control)])
    git(control, "config", "user.name", "BDB One Message Pilot")
    git(control, "config", "user.email", "one-message-pilot@example.invalid")
    return remote, control


def service_json(
    python_executable: str,
    repo_root: Path,
    bridge_config: Path,
    *command: str,
) -> dict[str, Any]:
    completed = run(
        [python_executable, "-m", "bdb_bridge", "bridge", *command, "--config", str(bridge_config), "--json"],
        cwd=repo_root,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(str(completed.stderr))
    return load_json_output(completed)


def parse_native_response(output: bytes) -> dict[str, Any]:
    stream = io.BytesIO(output)
    response = read_native_message(stream)
    if response is None:
        raise RuntimeError("Native Host returned no framed response")
    if stream.read(1) != b"":
        raise RuntimeError("Native Host returned unexpected trailing bytes")
    return response


def submit_patch(
    *,
    repo_root: Path,
    python_executable: str,
    native_config: Path,
    request_id: str,
    operations: list[dict[str, Any]],
    timeout: float,
    repo_alias: str = ALIAS,
    profile_id: str = "poc_pytest",
) -> dict[str, Any]:
    action = {
        "schema": "bdb-action-v1",
        "repo_alias": repo_alias,
        "operation": "multi_file_patch",
        "expected_revision": 0,
        "payload": {
            "profile_id": profile_id,
            "patch": {"schema": "bdb-multi-file-patch-v1", "operations": operations},
        },
    }
    request = {
        "schema": "bdb-native-request-v1",
        "request_id": request_id,
        "action": "submit_action",
        "wait_seconds": min(120.0, timeout),
        "bdb_action": action,
    }
    completed = run(
        [python_executable, "-c", _NATIVE_SHIM, ORIGIN, "--config", str(native_config)],
        cwd=repo_root,
        input_bytes=encode_native_message(request),
    )
    response = parse_native_response(bytes(completed.stdout))
    if response.get("status") != "completed" or not isinstance(response.get("result"), dict):
        raise RuntimeError(f"Native action did not complete with a result: {response}")
    return response


def assert_clean_checkout(path: Path, *, label: str) -> None:
    if git(path, "status", "--porcelain=v1"):
        raise RuntimeError(f"{label} is dirty")
