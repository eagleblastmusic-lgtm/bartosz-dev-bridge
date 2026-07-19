from __future__ import annotations

import time
from typing import Any

from bdb_bridge import one_message_pilot as pilot


_ORIGINAL_WAIT_UNTIL = pilot.wait_until


def _wait_until_with_startup_grace(description: str, *args: Any, **kwargs: Any) -> Any:
    if description == "Bridge RUNNING":
        time.sleep(1.0)
    return _ORIGINAL_WAIT_UNTIL(description, *args, **kwargs)


def main() -> int:
    pilot.wait_until = _wait_until_with_startup_grace
    return pilot.main()


if __name__ == "__main__":
    raise SystemExit(main())
