from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .protocol import BridgeError


BDB_RUNTIME_VERSION = "0.4.3"
SERVICE_STARTED_EVENT = "service.started"


def service_runtime_status(journal_path: str | Path) -> dict[str, Any]:
    database = Path(journal_path).expanduser().resolve(strict=False)
    base = {
        "expected_version": BDB_RUNTIME_VERSION,
        "compatible": False,
        "instance_id": None,
        "runtime_version": None,
    }
    if not database.is_file() or database.is_symlink():
        return {**base, "reason": "journal_unavailable"}
    try:
        connection = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            active = connection.execute(
                """
                SELECT instance_id
                FROM service_instances
                WHERE state IN ('running', 'stopping')
                """
            ).fetchall()
            if len(active) != 1:
                reason = "service_offline" if not active else "multiple_active_services"
                return {**base, "reason": reason}
            instance_id = active[0][0]
            events = connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE event_type = ?
                ORDER BY event_id DESC
                LIMIT 100
                """,
                (SERVICE_STARTED_EVENT,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return {**base, "reason": "journal_unavailable"}

    for row in events:
        if not isinstance(row[0], str):
            continue
        try:
            payload = json.loads(row[0])
        except (json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(payload, dict) or payload.get("instance_id") != instance_id:
            continue
        runtime_version = payload.get("runtime_version")
        compatible = runtime_version == BDB_RUNTIME_VERSION
        reason = None if compatible else (
            "version_mismatch" if isinstance(runtime_version, str) else "runtime_version_missing"
        )
        return {
            **base,
            "compatible": compatible,
            "instance_id": instance_id,
            "runtime_version": runtime_version if isinstance(runtime_version, str) else None,
            "reason": reason,
        }
    return {
        **base,
        "instance_id": instance_id,
        "reason": "runtime_version_missing",
    }


def require_compatible_service_runtime(journal_path: str | Path) -> dict[str, Any]:
    status = service_runtime_status(journal_path)
    if status["compatible"] is not True:
        raise BridgeError(
            "bridge_restart_required",
            "The active Bridge worker is missing or uses a different runtime version; restart the BDB session",
        )
    return status
