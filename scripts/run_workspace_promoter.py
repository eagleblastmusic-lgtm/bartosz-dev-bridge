from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from bdb_bridge.config import BridgeConfig
from bdb_bridge.instance_lock import InstanceLock
from bdb_bridge.protocol import BridgeError
from bdb_bridge.workspace_promoter import WorkspacePromoter, WorkspacePromotionWatcher


def _write_pid(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(f"{os.getpid()}\n", encoding="ascii", newline="\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote successful BDB worktrees into a local checkout")
    parser.add_argument("--config", required=True, help="Bridge config JSON")
    parser.add_argument("--once", action="store_true", help="Scan once and exit")
    parser.add_argument("--initialize-existing", action="store_true", help="Mark current results as pre-existing")
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--pid-file", help="Optional managed PID file")
    parser.add_argument("--stop-file", help="Optional cooperative stop-request file")
    args = parser.parse_args()

    if not 0.05 <= args.poll_seconds <= 30.0:
        parser.error("--poll-seconds must be between 0.05 and 30")

    config_path = Path(args.config).expanduser().resolve(strict=True)
    config = BridgeConfig.from_json(config_path)
    runtime = Path(config.runtime_dir)
    pid_file = Path(args.pid_file).expanduser().resolve(strict=False) if args.pid_file else None
    stop_file = Path(args.stop_file).expanduser().resolve(strict=False) if args.stop_file else None
    if pid_file is not None and pid_file.parent != runtime:
        parser.error("--pid-file must stay directly inside runtime_dir")
    if stop_file is not None and stop_file.parent != runtime:
        parser.error("--stop-file must stay directly inside runtime_dir")

    lock = InstanceLock(runtime / "workspace-promoter.lock")
    try:
        lock.acquire()
    except BridgeError as exc:
        sys.stderr.write(f"Workspace promoter cannot acquire its lock: {exc}\n")
        return 1

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    try:
        signal.signal(signal.SIGTERM, request_stop)
    except (AttributeError, ValueError):
        pass
    try:
        signal.signal(signal.SIGBREAK, request_stop)
    except (AttributeError, ValueError):
        pass

    try:
        runtime.mkdir(parents=True, exist_ok=True)
        if stop_file is not None:
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass
        if pid_file is not None:
            _write_pid(pid_file)

        promoter = WorkspacePromoter(config)
        watcher = WorkspacePromotionWatcher(promoter)
        initialized = watcher.initialize_existing()
        if args.initialize_existing:
            print(json.dumps({"status": "initialized", "ignored_existing": initialized}, sort_keys=True))
            return 0

        while True:
            outcomes = watcher.scan_once()
            for outcome in outcomes:
                print(json.dumps(outcome.as_dict(), ensure_ascii=False, sort_keys=True), flush=True)
            if args.once or stopping or (stop_file is not None and stop_file.exists()):
                return 0
            time.sleep(args.poll_seconds)
    except Exception as exc:
        code = getattr(exc, "code", None)
        sys.stderr.write(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": str(getattr(code, "value", code) or type(exc).__name__),
                    "detail": str(exc)[:500],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    finally:
        if pid_file is not None:
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
