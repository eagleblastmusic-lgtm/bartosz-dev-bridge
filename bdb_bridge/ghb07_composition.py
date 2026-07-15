from __future__ import annotations

import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .execution import SystemCrash
from .git_command_transport import GitCommandTransport
from .ingestion import CommandIngestor
from .instance_lock import InstanceLock
from .journal import Journal
from .models import BridgeErrorCode
from .protocol import BridgeError, parse_git_ref
from .result_outbox import OutboxProcessor, ResultCoordinator
from .result_transport import GitResultTransport
from .scheduler import SingleQueueScheduler
from .service import BridgeService


def run_foreground(config: BridgeConfig) -> int:
    from . import cli as legacy
    runtime = Path(config.runtime_dir)
    try:
        runtime.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        legacy._write_controlled_error(f"Failed to create runtime directory {runtime}", exc)
        return 1
    lock = InstanceLock(runtime / "bridge.instance.lock")
    try:
        lock.acquire()
    except BridgeError as exc:
        if exc.code == BridgeErrorCode.INSTANCE_ALREADY_RUNNING.value:
            sys.stderr.write("Error: Another bridge instance is already running\n")
        else:
            legacy._write_controlled_error("Failed to acquire lock", exc)
        return 1
    hook = legacy.get_cli_fault_hook()
    try:
        if hook:
            hook("AFTER_INSTANCE_LOCK_BEFORE_DB_START")
    except SystemCrash as exc:
        lock.release()
        legacy._write_controlled_error("Controlled lifecycle fault", exc)
        return 2
    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        lock.release()
        legacy._write_controlled_error("Failed to open journal", exc)
        return 1
    try:
        cmd_remote, cmd_branch = parse_git_ref(config.commands_ref)
        res_remote, _ = parse_git_ref(config.results_ref)
        transport = GitCommandTransport(
            config.control_repo_path, remote=cmd_remote or "origin", commands_branch=cmd_branch
        )
        ingestor = CommandIngestor(journal, transport, fault_hook=hook)
        scheduler = SingleQueueScheduler(journal)
        result_transport = GitResultTransport(config, remote_name=res_remote or "origin")
        outbox = OutboxProcessor(journal, result_transport, fault_hook=hook)
        coordinator = ResultCoordinator(config, journal, outbox, fault_hook=hook)
        service = BridgeService(
            config=config, journal=journal, ingestor=ingestor, scheduler=scheduler,
            result_coordinator=coordinator, outbox_processor=outbox,
            instance_lock=lock, fault_hook=hook,
        )
    except Exception as exc:
        journal.close(); lock.release()
        legacy._write_controlled_error("Failed to initialize service dependencies", exc)
        return 1
    instance_id = f"inst-{uuid.uuid4()}"
    def stop(signum: int, frame: Any) -> None:
        sys.stderr.write(f"Received signal {signum}, initiating graceful shutdown...\n")
        service.request_stop()
    signal.signal(signal.SIGINT, stop)
    for name in ("SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, stop)
    try:
        outcome = service.run(instance_id)
        if outcome.exit_code:
            sys.stderr.write(f"Service stopped with error: {outcome.error}\n")
        return outcome.exit_code
    finally:
        journal.close()
        lock.release()
