from __future__ import annotations

import subprocess
import sys
import time

from bdb_operator.runner import SubprocessCommandRunner


def test_runner_does_not_wait_for_descendant_holding_standard_streams() -> None:
    child_code = "import time; time.sleep(2.0)"
    parent_code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr, close_fds=False); "
        "print('parent-complete', flush=True)"
    )

    started = time.perf_counter()
    completed = SubprocessCommandRunner().run(
        [sys.executable, "-c", parent_code],
        timeout_seconds=5.0,
    )
    elapsed = time.perf_counter() - started

    assert completed.returncode == 0
    assert completed.stdout == "parent-complete\n"
    assert completed.stderr == ""
    assert elapsed < 1.5
