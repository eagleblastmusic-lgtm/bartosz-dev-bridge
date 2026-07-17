from __future__ import annotations

import sys
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


def main() -> int:
    pilot.run = _checked_run
    pilot.load_json_output = _checked_load_json_output
    return pilot.main()


if __name__ == "__main__":
    raise SystemExit(main())
