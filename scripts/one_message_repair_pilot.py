from __future__ import annotations

import json
import time
from typing import Any

from bdb_bridge import one_message_pilot as pilot


_STOP_MESSAGES = frozenset(
    {
        "Graceful stop request sent successfully.",
        "Service is already OFFLINE.",
        "Service status is STALE. Cannot stop gracefully. Please restart the service.",
        "Service stop is already in progress.",
    }
)
_ORIGINAL_WAIT_UNTIL = pilot.wait_until
_ORIGINAL_LOAD_JSON_OUTPUT = pilot.load_json_output


def _wait_until_with_startup_grace(description: str, *args: Any, **kwargs: Any) -> Any:
    if description == "Bridge RUNNING":
        time.sleep(1.0)
    return _ORIGINAL_WAIT_UNTIL(description, *args, **kwargs)


def _load_json_or_stop_message(completed: Any) -> dict[str, Any]:
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


def main() -> int:
    pilot.wait_until = _wait_until_with_startup_grace
    pilot.load_json_output = _load_json_or_stop_message
    return pilot.main()


if __name__ == "__main__":
    raise SystemExit(main())
