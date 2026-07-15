from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

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
from .protocol import BridgeError


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

    if args.command == "bridge":
        if args.bridge_command == "start":
            handle_start(config, config_path, args.foreground, args.background)
        elif args.bridge_command == "stop":
            handle_stop(config)
        elif args.bridge_command == "status":
            handle_status(config, args.json)


def handle_start(config: BridgeConfig, config_path: Path, foreground: bool, background: bool) -> None:
    if foreground and background:
        sys.stderr.write("Error: --foreground and --background are mutually exclusive\n")
        sys.exit(1)

    if background:
        if sys.platform != "win32":
            sys.stderr.write("Error: --background mode is only supported on Windows\n")
            sys.exit(1)
        run_background(config, config_path)
        return

    run_foreground(config)


def run_foreground(config: BridgeConfig) -> None:
    runtime_dir = Path(config.runtime_dir)
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        sys.stderr.write(f"Failed to create runtime directory {runtime_dir}: {exc}\n")
        sys.exit(1)

    lock_file = runtime_dir / "bridge.instance.lock"
    lock = InstanceLock(lock_file)
    try:
        lock.acquire()
    except BridgeError as exc:
        if exc.code == BridgeErrorCode.INSTANCE_ALREADY_RUNNING.value:
            sys.stderr.write("Error: Another bridge instance is already running\n")
            sys.exit(1)
        sys.stderr.write(f"Failed to acquire lock: {exc}\n")
        sys.exit(1)

    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        lock.release()
        sys.stderr.write(f"Failed to open journal: {exc}\n")
        sys.exit(1)

    try:
        transport = GitCommandTransport(
            config.control_repo_path,
            remote=config.commands_ref.split("/")[0],
            commands_branch=config.commands_ref.split("/")[-1],
        )
        ingestor = CommandIngestor(journal, transport)
        scheduler = SingleQueueScheduler(journal)
        
        res_transport = GitResultTransport(
            repo_path=config.control_repo_path,
            remote=config.results_ref.split("/")[0],
            results_branch=config.results_ref.split("/")[-1],
        )
        outbox_processor = OutboxProcessor(journal, res_transport)
        result_coordinator = ResultCoordinator(config, journal, outbox_processor)
    except Exception as exc:
        journal.close()
        lock.release()
        sys.stderr.write(f"Failed to initialize service dependencies: {exc}\n")
        sys.exit(1)

    service = BridgeService(
        config=config,
        journal=journal,
        ingestor=ingestor,
        scheduler=scheduler,
        result_coordinator=result_coordinator,
        outbox_processor=outbox_processor,
        instance_lock=lock,
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
            sys.exit(outcome.exit_code)
    finally:
        journal.close()
        lock.release()


def run_background(config: BridgeConfig, config_path: Path) -> None:
    try:
        journal = Journal.open(config.journal_path)
        reader = ServiceStatusReader(config)
        status = reader.get_status(journal)
        journal.close()
        if status.status in (ServiceStatus.RUNNING, ServiceStatus.STOPPING):
            sys.stderr.write("Error: Another bridge instance is already running\n")
            sys.exit(1)
    except Exception:
        pass

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
        sys.stderr.write(f"Failed to start background process: {exc}\n")
        sys.exit(1)

    start_time = time.time()
    success = False
    
    while time.time() - start_time < 5.0:
        time.sleep(0.2)
        try:
            journal = Journal.open(config.journal_path)
            reader = ServiceStatusReader(config)
            status = reader.get_status(journal)
            journal.close()
            if status.status == ServiceStatus.RUNNING:
                success = True
                break
            elif status.status in (ServiceStatus.STALE, ServiceStatus.OFFLINE):
                if proc.poll() is not None:
                    break
        except Exception:
            pass

    if success:
        print("Service started in background successfully.")
        sys.exit(0)
    else:
        sys.stderr.write("Error: Background service failed to start or transition to RUNNING status within timeout\n")
        sys.exit(1)


def handle_stop(config: BridgeConfig) -> None:
    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        sys.stderr.write(f"Failed to open journal: {exc}\n")
        sys.exit(1)

    try:
        reader = ServiceStatusReader(config)
        status = reader.get_status(journal)
        
        if status.status == ServiceStatus.OFFLINE:
            print("Service is already OFFLINE.")
            sys.exit(0)
            
        elif status.status == ServiceStatus.STALE:
            print("Service status is STALE. Cannot stop gracefully. Please restart the service or clear the lock.")
            sys.exit(0)
            
        elif status.status == ServiceStatus.STOPPING:
            print("Service stop is already in progress.")
            sys.exit(0)
            
        elif status.status == ServiceStatus.RUNNING:
            assert status.instance_id is not None
            outcome = journal.request_service_stop(status.instance_id)
            if outcome.stop_requested:
                print("Graceful stop request sent successfully.")
                sys.exit(0)
            else:
                sys.stderr.write("Failed to request stop.\n")
                sys.exit(1)
    finally:
        journal.close()


def handle_status(config: BridgeConfig, output_json: bool) -> None:
    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        sys.stderr.write(f"Failed to open journal: {exc}\n")
        sys.exit(1)

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
            print(json.dumps(data, indent=2))
        else:
            if snapshot.status == ServiceStatus.OFFLINE:
                print("Service is OFFLINE.")
            elif snapshot.status == ServiceStatus.RUNNING:
                print(f"Service is RUNNING (PID {snapshot.pid}, Instance ID {snapshot.instance_id}).")
            elif snapshot.status == ServiceStatus.STOPPING:
                print(f"Service is STOPPING (PID {snapshot.pid}, Instance ID {snapshot.instance_id}).")
            elif snapshot.status == ServiceStatus.STALE:
                print(f"Service is STALE: {snapshot.diagnostic or 'unknown reason'}")
    finally:
        journal.close()
