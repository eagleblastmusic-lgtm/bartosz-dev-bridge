from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import BridgeConfig
from .instance_lock import InstanceLock
from .journal import Journal
from .models import ServiceStatus
from .protocol import BridgeError, sanitize_diagnostics
from .service_status import ServiceStatusReader
from .session_finalization import SessionFinalizer
from .workspace_lifecycle import WorkspaceLifecycleCoordinator
from .workspace_types import WorkspaceLifecycleState
from .ghb07_composition import run_foreground
from . import cli as _legacy

_ORIGINAL_MAIN = _legacy.main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb")
    top = parser.add_subparsers(dest="command", required=True)
    bridge = top.add_parser("bridge")
    commands = bridge.add_subparsers(dest="bridge_command", required=True)
    session = commands.add_parser("session")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    finalize = session_commands.add_parser("finalize")
    finalize.add_argument("--config", required=True)
    finalize.add_argument("--session-id", required=True)
    workspace = commands.add_parser("workspace")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    status = workspace_commands.add_parser("status")
    status.add_argument("--config", required=True)
    status.add_argument("--session-id", required=True)
    status.add_argument("--json", action="store_true")
    preserve = workspace_commands.add_parser("preserve")
    preserve.add_argument("--config", required=True)
    preserve.add_argument("--session-id", required=True)
    cleanup = workspace_commands.add_parser("cleanup")
    cleanup.add_argument("--config", required=True)
    cleanup.add_argument("--session-id", required=True)
    cleanup.add_argument("--confirm-session-id", required=True)
    return parser


def _load(path: str) -> BridgeConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise BridgeError("invalid_config", f"Config file not found: {config_path}")
    return BridgeConfig.from_json(config_path)


def _offline(config: BridgeConfig) -> tuple[Journal, InstanceLock]:
    journal = Journal.open(config.journal_path)
    try:
        status = ServiceStatusReader(config).get_status(journal)
        if status.status is not ServiceStatus.OFFLINE or status.lock_held:
            raise BridgeError("instance_already_running", f"Service must be OFFLINE, got {status.status.value}")
        lock = InstanceLock(Path(config.runtime_dir) / "bridge.instance.lock")
        lock.acquire()
        if journal._connection.execute(
            "SELECT 1 FROM service_instances WHERE state IN ('running','stopping') LIMIT 1"
        ).fetchone() is not None:
            lock.release()
            raise BridgeError("instance_already_running", "Service became active during operator preflight")
        return journal, lock
    except BaseException:
        journal.close()
        raise


def _error(prefix: str, exc: Exception) -> int:
    _legacy._write_controlled_error(prefix, exc)
    return 1


def _finalize(config: BridgeConfig, session_id: str) -> int:
    journal = None
    lock = None
    try:
        journal, lock = _offline(config)
        outcome = SessionFinalizer(journal).finalize(session_id, lock_held=True)
        print(json.dumps({
            "finalized": outcome.finalized,
            "idempotent": outcome.idempotent,
            "session_id": outcome.session_id,
            "state": outcome.state.value,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        return _error("Session finalization failed", exc)
    finally:
        if journal is not None:
            journal.close()
        if lock is not None:
            lock.release()


def _status(config: BridgeConfig, session_id: str, output_json: bool) -> int:
    journal = None
    try:
        journal = Journal.open(config.journal_path)
        snapshot = WorkspaceLifecycleCoordinator(config, journal).status(session_id)
        if output_json:
            print(json.dumps(asdict(snapshot), sort_keys=True, separators=(",", ":")))
        else:
            print(
                f"Workspace {session_id}: session={snapshot.session_state} "
                f"state={snapshot.lifecycle_state} disposition={snapshot.disposition} "
                f"present={str(snapshot.present).lower()} eligible={str(snapshot.eligible).lower()}"
            )
            for reason in snapshot.blocking_reasons:
                print(f"- {reason}")
        return 0
    except Exception as exc:
        return _error("Workspace status failed", exc)
    finally:
        if journal is not None:
            journal.close()


def _preserve(config: BridgeConfig, session_id: str) -> int:
    journal = None
    try:
        journal = Journal.open(config.journal_path)
        record = WorkspaceLifecycleCoordinator(
            config, journal, fault_hook=_legacy.get_cli_fault_hook()
        ).preserve(session_id)
        print(json.dumps({
            "disposition": record.disposition.value,
            "session_id": session_id,
            "state": record.state.value,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        return _error("Workspace preserve failed", exc)
    finally:
        if journal is not None:
            journal.close()


def _cleanup(config: BridgeConfig, session_id: str, confirm: str) -> int:
    if confirm != session_id:
        sys.stderr.write(
            "Workspace cleanup failed [policy_denied]: "
            "--confirm-session-id must exactly match --session-id\n"
        )
        return 1
    journal = None
    lock = None
    try:
        journal, lock = _offline(config)
        outcome = WorkspaceLifecycleCoordinator(
            config, journal, fault_hook=_legacy.get_cli_fault_hook()
        ).cleanup(session_id, confirm_session_id=confirm, lock_held=True)
        if outcome.state is WorkspaceLifecycleState.BLOCKED:
            sys.stderr.write(
                "Workspace cleanup blocked [manual_reconciliation_required]: "
                + sanitize_diagnostics(outcome.diagnostic) + "\n"
            )
            return 1
        print(json.dumps({
            "already_removed": outcome.already_removed,
            "removed": outcome.removed,
            "session_id": outcome.session_id,
            "state": outcome.state.value,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        return _error("Workspace cleanup failed", exc)
    finally:
        if journal is not None:
            journal.close()
        if lock is not None:
            lock.release()


def main() -> None:
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] != "bridge" or argv[1] not in {"session", "workspace"}:
        _ORIGINAL_MAIN()
        return
    args = _parser().parse_args(argv)
    try:
        config = _load(args.config)
    except Exception as exc:
        sys.exit(_error("Failed to load config", exc))
    if args.bridge_command == "session":
        code = _finalize(config, args.session_id)
    elif args.workspace_command == "status":
        code = _status(config, args.session_id, args.json)
    elif args.workspace_command == "preserve":
        code = _preserve(config, args.session_id)
    else:
        code = _cleanup(config, args.session_id, args.confirm_session_id)
    sys.exit(code)


def install_cli() -> None:
    _legacy.run_foreground = run_foreground
    _legacy.main = main
