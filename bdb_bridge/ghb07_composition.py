from __future__ import annotations

import signal
import sqlite3
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
from .local_mirroring_outbox import LocalMirroringOutboxProcessor
from .local_result_sink import LocalResultSink
from .local_spool_transport import LocalSpoolTransport
from .local_wake import BridgeWakeWaiter
from .migrations import map_sqlite_error
from .models import BridgeErrorCode
from .priority_ingestion import PriorityCommandIngestor
from .protocol import BridgeError, parse_git_ref
from .result_outbox import OutboxProcessor, ResultCoordinator
from .result_transport import GitResultTransport
from .scheduler import SingleQueueScheduler
from .service import BridgeService


def reconcile_staged_result_after_restart(
    journal: Journal,
    outbox: OutboxProcessor,
):
    """Reconcile one durable RESULT_STAGED row while the process owns the OS lock.

    A process may crash after the remote push but before the local publication ACK.
    The pending outbox row then retains its lease. A new single-instance process may
    safely read the remote result immediately: identical bytes complete the local ACK,
    while an absent/unavailable remote remains pending without another push.
    """
    try:
        row = journal._connection.execute(
            """
            SELECT command_id
            FROM commands
            WHERE state = 'result_staged'
            ORDER BY created_at ASC, session_id ASC, sequence ASC, command_id ASC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="startup staged-result reconciliation") from exc
    if row is None:
        return None
    return outbox.process_command(str(row[0]))


def run_foreground(config: BridgeConfig) -> int:
    from . import cli as legacy

    runtime = Path(config.runtime_dir)
    try:
        runtime.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        legacy._write_controlled_error(f"Failed to create runtime directory {runtime}", exc)
        return 1
    lock = InstanceLock(runtime / "bridge.instance.lock")
    wake_waiter = BridgeWakeWaiter(runtime) if config.direct_spool_enabled else None
    try:
        lock.acquire()
    except BridgeError as exc:
        if wake_waiter is not None:
            wake_waiter.close()
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
        if wake_waiter is not None:
            wake_waiter.close()
        legacy._write_controlled_error("Controlled lifecycle fault", exc)
        return 2
    try:
        journal = Journal.open(config.journal_path)
    except Exception as exc:
        lock.release()
        if wake_waiter is not None:
            wake_waiter.close()
        legacy._write_controlled_error("Failed to open journal", exc)
        return 1
    try:
        cmd_remote, cmd_branch = parse_git_ref(config.commands_ref)
        res_remote, _ = parse_git_ref(config.results_ref)
        git_transport = GitCommandTransport(
            config.control_repo_path,
            remote=cmd_remote or "origin",
            commands_branch=cmd_branch,
        )
        git_ingestor = CommandIngestor(journal, git_transport, source_id="commands")
        if config.direct_spool_enabled:
            local_transport = LocalSpoolTransport(config.direct_spool_dir)
            local_ingestor = CommandIngestor(
                journal,
                local_transport,
                source_id="local-spool",
                backoff_base=0.1,
                backoff_max=1.0,
            )
            ingestor = PriorityCommandIngestor(local_ingestor, git_ingestor)
        else:
            ingestor = git_ingestor

        scheduler = SingleQueueScheduler(journal)
        result_transport = GitResultTransport(config, remote_name=res_remote or "origin")
        if config.direct_spool_enabled:
            outbox = LocalMirroringOutboxProcessor(
                journal,
                result_transport,
                fault_hook=hook,
                result_sink=LocalResultSink(config.direct_result_dir),
            )
        else:
            outbox = OutboxProcessor(journal, result_transport, fault_hook=hook)
        reconcile_staged_result_after_restart(journal, outbox)
        coordinator = ResultCoordinator(
            config,
            journal,
            outbox,
            fault_hook=hook,
            instance_lock=lock,
        )
        service_kwargs: dict[str, object] = {}
        if wake_waiter is not None:
            service_kwargs["waiter"] = wake_waiter
        service = BridgeService(
            config=config,
            journal=journal,
            ingestor=ingestor,
            scheduler=scheduler,
            result_coordinator=coordinator,
            outbox_processor=outbox,
            instance_lock=lock,
            fault_hook=hook,
            **service_kwargs,
        )
    except Exception as exc:
        journal.close()
        lock.release()
        if wake_waiter is not None:
            wake_waiter.close()
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
        try:
            outcome = service.run(instance_id)
        except SystemCrash as exc:
            legacy._write_controlled_error("Controlled recovery fault", exc)
            return 2
        if outcome.exit_code:
            sys.stderr.write(f"Service stopped with error: {outcome.error}\n")
        return outcome.exit_code
    finally:
        journal.close()
        lock.release()
        if wake_waiter is not None:
            wake_waiter.close()
