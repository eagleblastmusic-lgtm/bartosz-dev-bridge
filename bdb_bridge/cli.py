from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import BridgeConfig
from .journal import Journal
from .ingestion import CommandIngestor
from .scheduler import SingleQueueScheduler
from .result_outbox import OutboxProcessor, ResultCoordinator
from .git_command_transport import GitCommandTransport
from .result_transport import GitResultTransport
from .instance_lock import InstanceLock
from .service_status import ServiceStatusReader
from .service import BridgeService
from .models import ServiceStatus, BridgeErrorCode
from .protocol import BridgeError, parse_git_ref, sanitize_diagnostics
from .execution import SystemCrash


def get_cli_fault_hook() -> Callable[[str], None] | None:
    fault_point = os.environ.get("BDB_FAULT_POINT")
    if not fault_point:
        return None

    def hook(point: str) -> None:
        if point == fault_point:
            raise SystemCrash(f"Fault injection triggered at {point}")

    return hook


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return str(getattr(code, "value", code) or type(exc).__name__)


def _write_controlled_error(prefix: str, exc: Exception) -> None:
    detail = sanitize_diagnostics(str(exc)) or type(exc).__name__
    sys.stderr.write(f"{prefix} [{_error_code(exc)}]: {detail}\n")


def main() -> None:
    parser = argparse.ArgumentParser(prog="bdb")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bridge_parser = subparsers.add_parser("bridge")
    bridge_sub = bridge_parser.add_subparsers(dest="bridge_command", required=True)

    start_parser = bridge_sub.add_parser("start")
    start_parser.add_argument("--config", required=True, type=str, help="Path to config JSON")
    start_parser.add_argument("--foreground", action="store_true", help="Run service in foreground")
    start_parser.add_argument("--background", action="store_true", help="Run service in background (Windows only)")

    stop_parser = bridge_sub.add_parser("stop")
    stop_parser.add_argument("--config", required=True, type=str, help="Path to config JSON")

    status_parser = bridge_sub.add_parser("status")
    status_parser.add_argument("--config", required=True, type=str, help="Path to config JSON")
    status_parser.add_argument("--json", action="store_true", help="Output status as machine-readable JSON")

    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        sys.stderr.write(f"Config file not found: {config_path}\n")
        sys.exit(1)

    try:
        config = BridgeConfig.from_json(config_path)
    except Exception as exc:
        sys.stderr.write(f"Failed to load config: {exc}\n")
        sys.exit(1)

    code = 0
    if args.command == "bridge":
        if args.bridge_command == "start":
            code = handle_start(config, config_path, args.foreground, args.background)
        elif args.bridge_command == "stop":
            code = handle_stop(config)
        elif args.bridge_command == "status":
            code = handle_status(config, args.json)
    sys.exit(code)


def handle_start(config: BridgeConfig, config_path: Path, foreground: bool, background: bool) -> int:
    if foreground and background:
        sys.stderr.write("Error: --foreground and --background are mutually exclusive\n")
        return 1

    if background:
        if sys.platform != "win32":
            sys.stderr.write("Error: --background mode is only supported on Windows\n")
            return 1
        return run_background(config, config_path)

    return run_foreground(config)


def run_foreground(config: BridgeConfig) -> int:
    runtime_dir = Path(config.runtime_dir)
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _write_controlled_error(f"Failed to create runtime directory {runtime_dir}", exc)
        return 1

    lock_file = runtime_dir / "bridge.instance.lock"
    lock = InstanceLock(lock_file)
    try:
        lock.acquire()
    except BridgeError as exc:
        if exc.code == BridgeErrorCode.INSTANCE_ALREADY_RUNNING.value:
            sys.stderr.write("Error: Another bridge instance is already running\n")
            return 1
        _write_controlled_error("Failed to acquire lock", exc)
        return 1

    hook = get_cli_fault_hook()
    try:
        if hook:
            hook("AFTER_INSTANCE_LOCK_BEFORE_DB_START")
    except SystemCrash as exc:
        # This fault occurs before any active service row is created. Release the
        # process lock explicitly and return a controlled, bounded diagnostic.
        lock.release()
        _write_controlled_error("Controlled lifecycle fault", exc)
        return 2

    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        lock.release()
        _write_controlled_error("Failed to open journal", exc)
        return 1

    try:
        cmd_remote, cmd_branch = parse_git_ref(config.commands_ref)
        if not cmd_remote:
            cmd_remote = "origin"

        res_remote, _res_branch = parse_git_ref(config.results_ref)
        if not res_remote:
            res_remote = "origin"

        transport = GitCommandTransport(
            config.control_repo_path,
            remote=cmd_remote,
            commands_branch=cmd_branch,
        )
        ingestor = CommandIngestor(journal, transport)
        scheduler = SingleQueueScheduler(journal)

        res_transport = GitResultTransport(
            config,
            remote_name=res_remote,
        )
        outbox_processor = OutboxProcessor(journal, res_transport)
        result_coordinator = ResultCoordinator(config, journal, outbox_processor)
    except Exception as exc:
        journal.close()
        lock.release()
        _write_controlled_error("Failed to initialize service dependencies", exc)
        return 1

    service = BridgeService(
        config=config,
        journal=journal,
        ingestor=ingestor,
        scheduler=scheduler,
        result_coordinator=result_coordinator,
        outbox_processor=outbox_processor,
        instance_lock=lock,
        fault_hook=hook,
    )

    import signal
    import uuid

    instance_id = f"inst-{uuid.uuid4()}"

    def signal_handler(signum: int, frame: Any) -> None:
        sys.stderr.write(f"Received signal {signum}, initiating graceful shutdown...\n")
        service.request_stop()

    signal.signal(signal.SIGINT, signal_handler)
    try:
        signal.signal(signal.SIGBREAK, signal_handler)
    except AttributeError:
        pass
    try:
        signal.signal(signal.SIGTERM, signal_handler)
    except AttributeError:
        pass

    try:
        outcome = service.run(instance_id)
        if outcome.exit_code != 0:
            sys.stderr.write(f"Service stopped with error: {outcome.error}\n")
            return outcome.exit_code
        return 0
    finally:
        journal.close()
        lock.release()


def _background_preflight(config: BridgeConfig) -> tuple[int, str | None]:
    journal: Journal | None = None
    try:
        journal = Journal.open(config.journal_path)
        status = ServiceStatusReader(config).get_status(journal)
    except Exception as exc:
        # A corrupt or unsupported journal, permission failure, broken lock
        # backend, or status-reader failure must prevent child creation.
        detail = sanitize_diagnostics(str(exc)) or type(exc).__name__
        return 1, f"Background preflight failed [{_error_code(exc)}]: {detail}"
    finally:
        if journal is not None:
            try:
                journal.close()
            except Exception:
                pass

    if status.status in (ServiceStatus.RUNNING, ServiceStatus.STOPPING) or status.lock_held:
        return 1, "Another bridge instance is already running"
    return 0, None


def _read_background_status(config: BridgeConfig):
    journal: Journal | None = None
    try:
        journal = Journal.open(config.journal_path)
        return ServiceStatusReader(config).get_status(journal)
    finally:
        if journal is not None:
            journal.close()


def run_background(config: BridgeConfig, config_path: Path) -> int:
    preflight_code, preflight_error = _background_preflight(config)
    if preflight_code != 0:
        sys.stderr.write(f"Error: {preflight_error}\n")
        return preflight_code

    import subprocess

    cmd = [
        sys.executable,
        "-m", "bdb_bridge",
        "bridge", "start",
        "--config", str(config_path),
        "--foreground",
    ]

    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            cmd,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
        )
    except Exception as exc:
        _write_controlled_error("Failed to start background process", exc)
        return 1

    start_time = time.time()
    success = False
    last_error: str | None = None

    while time.time() - start_time < 5.0:
        time.sleep(0.2)
        try:
            status = _read_background_status(config)
        except Exception as exc:
            last_error = f"status verification failed [{_error_code(exc)}]: {sanitize_diagnostics(str(exc)) or type(exc).__name__}"
            break

        if status.status == ServiceStatus.RUNNING:
            success = True
            break
        if status.status in (ServiceStatus.STALE, ServiceStatus.OFFLINE) and proc.poll() is not None:
            break

    if success:
        print("Service started in background successfully.")
        return 0

    diagnostic = f": {last_error}" if last_error else ""
    sys.stderr.write(
        "Error: Background service failed to start or transition to RUNNING status within timeout"
        f"{diagnostic}\n"
    )
    return 1


def handle_stop(config: BridgeConfig) -> int:
    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        _write_controlled_error("Failed to open journal", exc)
        return 1

    try:
        reader = ServiceStatusReader(config)
        status = reader.get_status(journal)

        if status.status == ServiceStatus.OFFLINE:
            print("Service is already OFFLINE.")
            return 0

        if status.status == ServiceStatus.STALE:
            print("Service status is STALE. Cannot stop gracefully. Please restart the service.")
            return 0

        if status.status == ServiceStatus.STOPPING:
            print("Service stop is already in progress.")
            return 0

        if status.status == ServiceStatus.RUNNING:
            assert status.instance_id is not None
            outcome = journal.request_service_stop(status.instance_id)
            if outcome.stop_requested:
                print("Graceful stop request sent successfully.")
                return 0
            sys.stderr.write("Failed to request stop.\n")
            return 1
        return 0
    finally:
        journal.close()


def handle_status(config: BridgeConfig, output_json: bool) -> int:
    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        _write_controlled_error("Failed to open journal", exc)
        return 1

    try:
        reader = ServiceStatusReader(config)
        snapshot = reader.get_status(journal)

        if output_json:
            data = {
                "status": snapshot.status.value,
                "instance_id": snapshot.instance_id,
                "pid": snapshot.pid,
                "started_at": snapshot.started_at,
                "heartbeat_at": snapshot.heartbeat_at,
                "heartbeat_age_seconds": snapshot.heartbeat_age_seconds,
                "lock_held": snapshot.lock_held,
                "pid_alive": snapshot.pid_alive,
                "stop_requested": snapshot.stop_requested,
                "diagnostic": snapshot.diagnostic,
            }
            print(json.dumps(data, sort_keys=True, separators=(",", ":")))
        else:
            if snapshot.status == ServiceStatus.OFFLINE:
                print("Service is OFFLINE.")
            elif snapshot.status == ServiceStatus.RUNNING:
                print(f"Service is RUNNING (PID {snapshot.pid}, Instance ID {snapshot.instance_id}).")
            elif snapshot.status == ServiceStatus.STOPPING:
                print(f"Service is STOPPING (PID {snapshot.pid}, Instance ID {snapshot.instance_id}).")
            elif snapshot.status == ServiceStatus.STALE:
                print(f"Service is STALE: {snapshot.diagnostic or 'unknown reason'}")
        return 0
    finally:
        journal.close()
