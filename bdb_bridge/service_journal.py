from __future__ import annotations

import sqlite3
from typing import Any

from .models import (
    BridgeErrorCode,
    ServiceInstanceState,
    ServiceInstanceRecord,
    ServiceStatus,
    ServiceStatusSnapshot,
    StopRequestOutcome,
    CommandRecord,
)
from .protocol import BridgeError
from .migrations import map_sqlite_error


def _row_to_service_instance(row: tuple[Any, ...]) -> ServiceInstanceRecord:
    return ServiceInstanceRecord(
        instance_id=row[0],
        pid=row[1],
        state=ServiceInstanceState(row[2]),
        started_at=row[3],
        heartbeat_at=row[4],
        stop_requested_at=row[5],
        stopped_at=row[6],
        exit_code=row[7],
        last_error=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def get_service_instance(self: Any, instance_id: str) -> ServiceInstanceRecord | None:
    self._ensure_open()
    row = self._conn.execute(
        """
        SELECT instance_id, pid, state, started_at, heartbeat_at,
               stop_requested_at, stopped_at, exit_code, last_error,
               created_at, updated_at
        FROM service_instances WHERE instance_id = ?
        """,
        (instance_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_service_instance(row)


def get_active_service_instance(self: Any) -> ServiceInstanceRecord | None:
    self._ensure_open()
    row = self._conn.execute(
        """
        SELECT instance_id, pid, state, started_at, heartbeat_at,
               stop_requested_at, stopped_at, exit_code, last_error,
               created_at, updated_at
        FROM service_instances
        WHERE state IN ('running', 'stopping')
        """
    ).fetchone()
    if row is None:
        return None
    return _row_to_service_instance(row)


def get_latest_service_instance(self: Any) -> ServiceInstanceRecord | None:
    self._ensure_open()
    row = self._conn.execute(
        """
        SELECT instance_id, pid, state, started_at, heartbeat_at,
               stop_requested_at, stopped_at, exit_code, last_error,
               created_at, updated_at
        FROM service_instances
        ORDER BY started_at DESC, instance_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return _row_to_service_instance(row)


def start_service_instance(
    self: Any, instance_id: str, pid: int, started_at: str
) -> ServiceInstanceRecord:
    self._ensure_open()
    if not instance_id or not isinstance(instance_id, str):
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Invalid instance_id")
    if pid <= 0:
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "PID must be positive")
    if not started_at or not isinstance(started_at, str):
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Invalid started_at timestamp")

    now = self._now_fn()
    try:
        with self._transaction():
            active = self.get_active_service_instance()
            if active is not None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Another active service instance ({active.instance_id}, PID {active.pid}) is already running",
                )

            self._conn.execute(
                """
                INSERT INTO service_instances (
                    instance_id, pid, state, started_at, heartbeat_at,
                    stop_requested_at, stopped_at, exit_code, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    instance_id,
                    pid,
                    ServiceInstanceState.RUNNING.value,
                    started_at,
                    started_at,
                    now,
                    now,
                ),
            )

            self._append_event_in_transaction(
                session_id=None,
                command_id=None,
                event_type="service.started",
                payload={"instance_id": instance_id, "pid": pid},
                created_at=now,
            )
    except sqlite3.IntegrityError as exc:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CONFLICT,
            f"Active service constraint violated: {exc}",
        ) from exc
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="start service instance") from exc

    record = self.get_service_instance(instance_id)
    assert record is not None
    return record


def mark_abandoned_service_instances_stale(self: Any, diagnostic: str) -> int:
    self._ensure_open()
    diag = (diagnostic or "Abandoned instance").strip()[:1000]
    now = self._now_fn()

    try:
        with self._transaction():
            rows = self._conn.execute(
                "SELECT instance_id, pid FROM service_instances WHERE state IN ('running', 'stopping')"
            ).fetchall()

            count = 0
            for row in rows:
                inst_id, pid = row[0], row[1]
                self._conn.execute(
                    """
                    UPDATE service_instances
                    SET state = ?, last_error = ?, updated_at = ?
                    WHERE instance_id = ?
                    """,
                    (ServiceInstanceState.STALE.value, diag, now, inst_id),
                )
                self._append_event_in_transaction(
                    session_id=None,
                    command_id=None,
                    event_type="service.stale_detected",
                    payload={"instance_id": inst_id, "pid": pid, "diagnostic": diag},
                    created_at=now,
                )
                count += 1
            return count
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="mark stale service instances") from exc


def heartbeat_service_instance(self: Any, instance_id: str) -> None:
    self._ensure_open()
    now = self._now_fn()
    try:
        with self._transaction():
            cursor = self._conn.execute(
                """
                UPDATE service_instances
                SET heartbeat_at = ?, updated_at = ?
                WHERE instance_id = ? AND state IN ('running', 'stopping')
                """,
                (now, now, instance_id),
            )
            if cursor.rowcount != 1:
                row = self._conn.execute(
                    "SELECT state FROM service_instances WHERE instance_id = ?",
                    (instance_id,),
                ).fetchone()
                if row is None:
                    raise BridgeError(
                        BridgeErrorCode.JOURNAL_CONFLICT,
                        f"Heartbeat failed: Service instance {instance_id} not found",
                    )
                state = row[0]
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Heartbeat failed: Service instance {instance_id} is in state {state!r} (cannot heartbeat non-active instance)",
                )
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="heartbeat service instance") from exc


def request_service_stop(self: Any, instance_id: str) -> StopRequestOutcome:
    self._ensure_open()
    now = self._now_fn()
    try:
        with self._transaction():
            row = self._conn.execute(
                "SELECT instance_id, state, stop_requested_at FROM service_instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            if row is None:
                return StopRequestOutcome(
                    instance_id=instance_id,
                    status=ServiceStatus.OFFLINE,
                    stop_requested=False,
                )

            state = row[1]

            if state in ("stopped", "stale", "failed"):
                return StopRequestOutcome(
                    instance_id=instance_id,
                    status=ServiceStatus.OFFLINE,
                    stop_requested=False,
                )

            if state == "running":
                self._conn.execute(
                    """
                    UPDATE service_instances
                    SET state = ?, stop_requested_at = ?, updated_at = ?
                    WHERE instance_id = ? AND state = 'running'
                    """,
                    (ServiceInstanceState.STOPPING.value, now, now, instance_id),
                )
                self._append_event_in_transaction(
                    session_id=None,
                    command_id=None,
                    event_type="service.stop_requested",
                    payload={"instance_id": instance_id},
                    created_at=now,
                )
                status = ServiceStatus.STOPPING
            else:
                status = ServiceStatus.STOPPING

            return StopRequestOutcome(
                instance_id=instance_id,
                status=status,
                stop_requested=True,
            )
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="request service stop") from exc


def mark_service_instance_stopped(self: Any, instance_id: str, exit_code: int) -> None:
    self._ensure_open()
    now = self._now_fn()
    try:
        with self._transaction():
            row = self._conn.execute(
                "SELECT state, pid FROM service_instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            if row is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Service instance {instance_id} not found",
                )
            state, pid = row[0], row[1]
            if state == "stopped":
                return

            if state not in ("running", "stopping"):
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Cannot mark service instance {instance_id} as stopped: current state is {state!r}",
                )

            self._conn.execute(
                """
                UPDATE service_instances
                SET state = ?, heartbeat_at = ?, stopped_at = ?, exit_code = ?, updated_at = ?
                WHERE instance_id = ?
                """,
                (
                    ServiceInstanceState.STOPPED.value,
                    now,
                    now,
                    exit_code,
                    now,
                    instance_id,
                ),
            )
            self._append_event_in_transaction(
                session_id=None,
                command_id=None,
                event_type="service.stopped",
                payload={"instance_id": instance_id, "pid": pid, "exit_code": exit_code},
                created_at=now,
            )
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="mark service stopped") from exc


def mark_service_instance_failed(self: Any, instance_id: str, error: str) -> None:
    self._ensure_open()
    now = self._now_fn()
    sanitized_error = str(error)[:1000]
    try:
        with self._transaction():
            row = self._conn.execute(
                "SELECT state, pid FROM service_instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            if row is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Service instance {instance_id} not found",
                )
            state, pid = row[0], row[1]
            if state == "failed":
                return

            if state not in ("running", "stopping"):
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Cannot mark service instance {instance_id} as failed: current state is {state!r}",
                )

            self._conn.execute(
                """
                UPDATE service_instances
                SET state = ?, heartbeat_at = ?, stopped_at = ?, exit_code = ?, last_error = ?, updated_at = ?
                WHERE instance_id = ?
                """,
                (
                    ServiceInstanceState.FAILED.value,
                    now,
                    now,
                    1,
                    sanitized_error,
                    now,
                    instance_id,
                ),
            )
            self._append_event_in_transaction(
                session_id=None,
                command_id=None,
                event_type="service.failed",
                payload={"instance_id": instance_id, "pid": pid, "error": sanitized_error},
                created_at=now,
            )
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="mark service failed") from exc


def get_recoverable_command(self: Any) -> CommandRecord | None:
    self._ensure_open()
    rows = self._conn.execute(
        """
        SELECT command_id, session_id, sequence, command_sha256, command_json,
               command_commit_sha, state, expected_revision, expected_state_hash,
               created_at, updated_at
        FROM commands
        WHERE state IN ('claimed', 'executing', 'effect_recorded')
        """
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CORRUPT,
            f"Database corruption: multiple active commands found ({len(rows)}), only one allowed.",
        )
    from .journal import _row_to_command
    return _row_to_command(rows[0])


def install_journal_service_api(journal_class: type[Any]) -> None:
    journal_class.get_service_instance = get_service_instance
    journal_class.get_active_service_instance = get_active_service_instance
    journal_class.get_latest_service_instance = get_latest_service_instance
    journal_class.start_service_instance = start_service_instance
    journal_class.mark_abandoned_service_instances_stale = mark_abandoned_service_instances_stale
    journal_class.heartbeat_service_instance = heartbeat_service_instance
    journal_class.request_service_stop = request_service_stop
    journal_class.mark_service_instance_stopped = mark_service_instance_stopped
    journal_class.mark_service_instance_failed = mark_service_instance_failed
    journal_class.get_recoverable_command = get_recoverable_command
