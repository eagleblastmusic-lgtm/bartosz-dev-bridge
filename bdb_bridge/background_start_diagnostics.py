from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any


_MAX_TAIL_BYTES = 65_536


def _bounded_tail(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - _MAX_TAIL_BYTES))
            raw = handle.read(_MAX_TAIL_BYTES)
        return raw.decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def install_background_start_diagnostics(cli_module: Any) -> None:
    """Preserve bounded startup diagnostics for detached Bridge failures."""

    if getattr(cli_module, "_bdb_background_start_diagnostics", False):
        return

    def run_background(config: Any, config_path: Path) -> int:
        preflight_code, preflight_error = cli_module._background_preflight(config)
        if preflight_code != 0:
            sys.stderr.write(f"Error: {preflight_error}\n")
            return preflight_code

        cmd = [
            sys.executable,
            "-m",
            "bdb_bridge",
            "bridge",
            "start",
            "--config",
            str(config_path),
            "--foreground",
        ]
        runtime_dir = Path(config.runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = runtime_dir / "bridge-background.stdout.log"
        stderr_path = runtime_dir / "bridge-background.stderr.log"

        creationflags = 0x00000200 | 0x00000008 | 0x08000000
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                proc = subprocess.Popen(
                    cmd,
                    creationflags=creationflags,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    close_fds=True,
                    shell=False,
                )
        except Exception as exc:
            cli_module._write_controlled_error("Failed to start background process", exc)
            return 1

        start_time = time.time()
        success = False
        last_error: str | None = None
        while time.time() - start_time < 5.0:
            time.sleep(0.2)
            try:
                status = cli_module._read_background_status(config)
            except Exception as exc:
                last_error = (
                    "status verification failed "
                    f"[{cli_module._error_code(exc)}]: "
                    f"{cli_module.sanitize_diagnostics(str(exc)) or type(exc).__name__}"
                )
                break
            if status.status == cli_module.ServiceStatus.RUNNING:
                success = True
                break
            if (
                status.status in (cli_module.ServiceStatus.STALE, cli_module.ServiceStatus.OFFLINE)
                and proc.poll() is not None
            ):
                break

        if success:
            print("Service started in background successfully.")
            return 0

        diagnostic = f": {last_error}" if last_error else ""
        stderr_tail = _bounded_tail(stderr_path)
        if stderr_tail:
            diagnostic += f"; stderr_tail={stderr_tail}"
        sys.stderr.write(
            "Error: Background service failed to start or transition to RUNNING status within timeout"
            f"{diagnostic}; stderr_log={stderr_path}\n"
        )
        return 1

    cli_module.run_background = run_background
    cli_module._bdb_background_start_diagnostics = True
