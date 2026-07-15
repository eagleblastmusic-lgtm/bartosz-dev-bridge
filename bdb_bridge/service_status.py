from __future__ import annotations

import ctypes
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    BridgeErrorCode,
    ServiceInstanceState,
    ServiceStatus,
    ServiceStatusSnapshot,
)
from .protocol import BridgeError
from .journal import Journal
from .instance_lock import InstanceLock


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    else:
        # Windows: OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION (0x1000)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            STILL_ACTIVE = 259
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                alive = (exit_code.value == STILL_ACTIVE)
            else:
                alive = False
            kernel32.CloseHandle(handle)
            return alive
        else:
            error = kernel32.GetLastError()
            # ERROR_ACCESS_DENIED = 5
            return error == 5


def parse_utc_timestamp(ts: str) -> datetime:
    cleaned = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


def is_lock_held(lock_path: Path) -> bool:
    lock = InstanceLock(lock_path)
    try:
        if lock.acquire():
            lock.release()
            return False
    except BridgeError as exc:
        if exc.code == BridgeErrorCode.INSTANCE_ALREADY_RUNNING.value:
            return True
        raise
    return False


class ServiceStatusReader:
    def __init__(self, config: Any) -> None:
        self.config = config

    def get_status(self, journal: Journal) -> ServiceStatusSnapshot:
        lock_path = self.config.runtime_dir / "bridge.instance.lock"
        lock_held = is_lock_held(lock_path)

        active = journal.get_active_service_instance()
        latest = journal.get_latest_service_instance()

        now_str = journal._now_fn()
        now_dt = parse_utc_timestamp(now_str)

        if active is None:
            inst_id = latest.instance_id if latest else None
            pid = latest.pid if latest else None
            started_at = latest.started_at if latest else None
            heartbeat_at = latest.heartbeat_at if latest else None
            age = (now_dt - parse_utc_timestamp(heartbeat_at)).total_seconds() if heartbeat_at else None
            
            if lock_held:
                return ServiceStatusSnapshot(
                    status=ServiceStatus.STALE,
                    instance_id=inst_id,
                    pid=pid,
                    started_at=started_at,
                    heartbeat_at=heartbeat_at,
                    heartbeat_age_seconds=age,
                    lock_held=True,
                    pid_alive=is_pid_alive(pid) if pid else None,
                    stop_requested=False,
                    diagnostic="Lock held but no active instance recorded in database",
                )
            else:
                return ServiceStatusSnapshot(
                    status=ServiceStatus.OFFLINE,
                    instance_id=inst_id,
                    pid=pid,
                    started_at=started_at,
                    heartbeat_at=heartbeat_at,
                    heartbeat_age_seconds=age,
                    lock_held=False,
                    pid_alive=is_pid_alive(pid) if pid else None,
                    stop_requested=False,
                    diagnostic=None,
                )

        inst_id = active.instance_id
        pid = active.pid
        started_at = active.started_at
        heartbeat_at = active.heartbeat_at
        heartbeat_age_seconds = (now_dt - parse_utc_timestamp(heartbeat_at)).total_seconds()
        pid_alive = is_pid_alive(pid)
        stop_requested = (active.state == ServiceInstanceState.STOPPING)

        is_stale = False
        diagnostic = None

        if not lock_held:
            is_stale = True
            diagnostic = "Active record in database but lock file is not locked"
        elif heartbeat_age_seconds > self.config.heartbeat_stale_seconds:
            is_stale = True
            diagnostic = f"Heartbeat age ({heartbeat_age_seconds:.2f}s) exceeded stale threshold ({self.config.heartbeat_stale_seconds}s)"
        elif pid_alive is False:
            is_stale = True
            diagnostic = f"PID {pid} is not alive"

        if is_stale:
            return ServiceStatusSnapshot(
                status=ServiceStatus.STALE,
                instance_id=inst_id,
                pid=pid,
                started_at=started_at,
                heartbeat_at=heartbeat_at,
                heartbeat_age_seconds=heartbeat_age_seconds,
                lock_held=lock_held,
                pid_alive=pid_alive,
                stop_requested=stop_requested,
                diagnostic=diagnostic,
            )

        if active.state == ServiceInstanceState.STOPPING:
            status = ServiceStatus.STOPPING
        else:
            status = ServiceStatus.RUNNING

        return ServiceStatusSnapshot(
            status=status,
            instance_id=inst_id,
            pid=pid,
            started_at=started_at,
            heartbeat_at=heartbeat_at,
            heartbeat_age_seconds=heartbeat_age_seconds,
            lock_held=lock_held,
            pid_alive=pid_alive,
            stop_requested=stop_requested,
            diagnostic=None,
        )
