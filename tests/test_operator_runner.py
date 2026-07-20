from __future__ import annotations

import subprocess
import sys
import time

from bdb_operator.runner import WINDOWS_CREATE_NO_WINDOW, SubprocessCommandRunner


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


def test_runner_uses_create_no_window_for_windows_helpers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    completed = SubprocessCommandRunner(platform_name="nt").run(
        ["python.exe", "-c", "pass"],
        timeout_seconds=5.0,
    )

    assert completed.returncode == 0
    assert captured["args"] == ["python.exe", "-c", "pass"]
    assert captured["creationflags"] == WINDOWS_CREATE_NO_WINDOW
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL


def test_runner_does_not_pass_windows_creation_flags_on_posix(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)

    SubprocessCommandRunner(platform_name="posix").run(
        ["python", "-c", "pass"],
        timeout_seconds=5.0,
    )

    assert "creationflags" not in captured
