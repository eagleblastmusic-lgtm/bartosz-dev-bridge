from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import OperatorApiError, OperatorErrorCode
from .models import utc_now_iso


EVENT_SCHEMA = "bdb-event-v1"
CURRENT_OPERATION_SCHEMA = "bdb-current-operation-v1"
LOG_SNAPSHOT_SCHEMA = "bdb-log-snapshot-v1"
WORKSPACE_STATE_SCHEMA = "bdb-workspace-loop-state-v1"
MAX_EVENT_LIMIT = 500
MAX_EVENT_PAYLOAD_CHARS = 16_384
MAX_LOG_BYTES = 65_536
MAX_LOG_LINES = 500
_ACTIVE_COMMAND_STATES = (
    "discovered",
    "validated",
    "claimed",
    "executing",
    "effect_recorded",
    "result_staged",
    "result_published",
)


@dataclass(frozen=True)
class ObservabilityWorkspace:
    root: Path
    alias: str
    journal_path: Path
    promoter_stdout: Path | None
    promoter_stderr: Path | None


class ObservabilityReader:
    """Read-only projection over the existing BDB Journal and declared log files."""

    def __init__(self, workspace: ObservabilityWorkspace) -> None:
        self.workspace = workspace

    @classmethod
    def from_workspace_root(cls, workspace_root: str | Path) -> "ObservabilityReader":
        root = Path(workspace_root).expanduser().resolve(strict=False)
        state_path = root / "workspace-loop-state.json"
        state = _read_json_object(
            state_path,
            missing_code=OperatorErrorCode.WORKSPACE_STATE_MISSING,
            invalid_code=OperatorErrorCode.WORKSPACE_STATE_INVALID,
            label="workspace loop state",
        )
        if state.get("schema") != WORKSPACE_STATE_SCHEMA:
            raise OperatorApiError(
                OperatorErrorCode.WORKSPACE_STATE_INVALID,
                "Workspace loop state schema is unsupported",
                details={"path": str(state_path), "schema": state.get("schema")},
            )
        alias = _required_string(state, "alias", state_path)
        bridge_config_path = Path(_required_string(state, "bridge_config", state_path)).expanduser().resolve(
            strict=False
        )
        bridge_config = _read_json_object(
            bridge_config_path,
            missing_code=OperatorErrorCode.OBSERVABILITY_CONFIG_MISSING,
            invalid_code=OperatorErrorCode.OBSERVABILITY_CONFIG_INVALID,
            label="bridge config",
        )
        journal_path = Path(_required_string(bridge_config, "journal_path", bridge_config_path)).expanduser().resolve(
            strict=False
        )
        return cls(
            ObservabilityWorkspace(
                root=root,
                alias=alias,
                journal_path=journal_path,
                promoter_stdout=_optional_path(state.get("promoter_stdout")),
                promoter_stderr=_optional_path(state.get("promoter_stderr")),
            )
        )

    def list_events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 100,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(after_event_id, int) or isinstance(after_event_id, bool) or after_event_id < 0:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_ARGUMENT,
                "after_event_id must be a non-negative integer",
                details={"after_event_id": after_event_id},
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_EVENT_LIMIT:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_ARGUMENT,
                f"limit must be between 1 and {MAX_EVENT_LIMIT}",
                details={"limit": limit},
            )
        for name, value in (("session_id", session_id), ("command_id", command_id)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise OperatorApiError(
                    OperatorErrorCode.INVALID_ARGUMENT,
                    f"{name} must be a non-empty string when provided",
                )

        clauses = ["event_id > ?"]
        parameters: list[Any] = [after_event_id]
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if command_id is not None:
            clauses.append("command_id = ?")
            parameters.append(command_id)
        parameters.append(limit + 1)
        query = f"""
            SELECT event_id, session_id, command_id, event_type, payload_json, created_at
            FROM events
            WHERE {' AND '.join(clauses)}
            ORDER BY event_id ASC
            LIMIT ?
        """
        with self._read_only_connection() as connection:
            rows = connection.execute(query, parameters).fetchall()

        has_more = len(rows) > limit
        selected = rows[:limit]
        events = [self._event_document(row) for row in selected]
        next_after = int(selected[-1]["event_id"]) if selected else after_event_id
        return {
            "project_alias": self.workspace.alias,
            "events": events,
            "cursor": {
                "after_event_id": after_event_id,
                "next_after_event_id": next_after,
                "has_more": has_more,
            },
            "filters": {"session_id": session_id, "command_id": command_id},
        }

    def current_operation(self) -> dict[str, Any]:
        placeholders = ", ".join("?" for _ in _ACTIVE_COMMAND_STATES)
        query = f"""
            SELECT
                c.command_id,
                c.session_id,
                c.sequence,
                c.state,
                c.command_json,
                c.created_at,
                c.updated_at,
                s.repository_id,
                s.state AS session_state,
                p.operation AS planned_operation,
                p.target_path,
                p.profile_id,
                w.revision AS workspace_revision,
                w.state_hash AS workspace_state_hash,
                r.status AS result_status,
                r.error_code
            FROM commands c
            JOIN sessions s ON s.session_id = c.session_id
            LEFT JOIN operation_plans p ON p.command_id = c.command_id
            LEFT JOIN workspaces w ON w.session_id = c.session_id
            LEFT JOIN results r ON r.command_id = c.command_id
            WHERE c.state IN ({placeholders})
            ORDER BY c.updated_at DESC, c.sequence DESC
            LIMIT 1
        """
        with self._read_only_connection() as connection:
            row = connection.execute(query, _ACTIVE_COMMAND_STATES).fetchone()

        return {
            "schema": CURRENT_OPERATION_SCHEMA,
            "project_alias": self.workspace.alias,
            "generated_at": utc_now_iso(),
            "active": row is not None,
            "operation": self._operation_document(row) if row is not None else None,
        }

    def log_snapshot(self, *, max_bytes: int = MAX_LOG_BYTES, max_lines: int = 200) -> dict[str, Any]:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_LOG_BYTES:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_ARGUMENT,
                f"max_bytes must be between 1 and {MAX_LOG_BYTES}",
                details={"max_bytes": max_bytes},
            )
        if not isinstance(max_lines, int) or isinstance(max_lines, bool) or not 1 <= max_lines <= MAX_LOG_LINES:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_ARGUMENT,
                f"max_lines must be between 1 and {MAX_LOG_LINES}",
                details={"max_lines": max_lines},
            )
        sources = [
            self._tail_log("promoter_stdout", self.workspace.promoter_stdout, max_bytes=max_bytes, max_lines=max_lines),
            self._tail_log("promoter_stderr", self.workspace.promoter_stderr, max_bytes=max_bytes, max_lines=max_lines),
        ]
        return {
            "schema": LOG_SNAPSHOT_SCHEMA,
            "project_alias": self.workspace.alias,
            "generated_at": utc_now_iso(),
            "limits": {"max_bytes_per_source": max_bytes, "max_lines_per_source": max_lines},
            "sources": sources,
        }

    @contextmanager
    def _read_only_connection(self) -> Iterator[sqlite3.Connection]:
        path = self.workspace.journal_path
        if not path.is_file():
            raise OperatorApiError(
                OperatorErrorCode.JOURNAL_MISSING,
                "BDB Journal is missing",
                details={"path": str(path)},
            )
        connection: sqlite3.Connection | None = None
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=1.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            yield connection
        except OperatorApiError:
            raise
        except sqlite3.Error as error:
            raise OperatorApiError(
                OperatorErrorCode.JOURNAL_UNAVAILABLE,
                "BDB Journal could not be read",
                details={"path": str(path), "reason": str(error)},
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def _event_document(self, row: sqlite3.Row) -> dict[str, Any]:
        event_type = str(row["event_type"])
        payload, payload_warning = _decode_payload(row["payload_json"])
        severity = _event_severity(event_type, payload, payload_warning)
        return {
            "schema": EVENT_SCHEMA,
            "event_id": f"journal:{self.workspace.alias}:{int(row['event_id'])}",
            "sequence": int(row["event_id"]),
            "event_type": event_type,
            "occurred_at": str(row["created_at"]),
            "source": "bridge",
            "severity": severity,
            "correlation_id": row["command_id"] or row["session_id"],
            "session_id": row["session_id"],
            "command_id": row["command_id"],
            "payload": payload,
        }

    def _operation_document(self, row: sqlite3.Row) -> dict[str, Any]:
        command = _decode_command_summary(row["command_json"])
        return {
            "command_id": row["command_id"],
            "session_id": row["session_id"],
            "sequence": int(row["sequence"]),
            "state": row["state"],
            "operation": row["planned_operation"] or command.get("operation"),
            "target_path": row["target_path"] or command.get("target_path"),
            "profile_id": row["profile_id"] or command.get("profile_id"),
            "repository_id": row["repository_id"],
            "session_state": row["session_state"],
            "workspace_revision": row["workspace_revision"],
            "workspace_state_hash": row["workspace_state_hash"],
            "result_status": row["result_status"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _tail_log(self, source: str, path: Path | None, *, max_bytes: int, max_lines: int) -> dict[str, Any]:
        if path is None:
            return {
                "source": source,
                "path": None,
                "exists": False,
                "size_bytes": 0,
                "modified_at": None,
                "truncated": False,
                "lines": [],
            }
        try:
            if not path.is_file():
                return {
                    "source": source,
                    "path": str(path),
                    "exists": False,
                    "size_bytes": 0,
                    "modified_at": None,
                    "truncated": False,
                    "lines": [],
                }
            stat = path.stat()
            start = max(0, stat.st_size - max_bytes)
            with path.open("rb") as handle:
                handle.seek(start)
                raw = handle.read(max_bytes)
            text = raw.decode("utf-8", errors="replace")
            if start > 0 and text:
                separator = text.find("\n")
                text = text[separator + 1 :] if separator >= 0 else ""
            all_lines = text.splitlines()
            line_truncated = len(all_lines) > max_lines
            selected = all_lines[-max_lines:]
            return {
                "source": source,
                "path": str(path),
                "exists": True,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                "truncated": start > 0 or line_truncated,
                "lines": selected,
            }
        except OSError as error:
            raise OperatorApiError(
                OperatorErrorCode.LOG_READ_FAILED,
                "Declared BDB log could not be read",
                details={"source": source, "path": str(path), "reason": str(error)},
            ) from error


def _read_json_object(
    path: Path,
    *,
    missing_code: OperatorErrorCode,
    invalid_code: OperatorErrorCode,
    label: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise OperatorApiError(missing_code, f"{label.capitalize()} is missing", details={"path": str(path)})
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperatorApiError(
            invalid_code,
            f"{label.capitalize()} is not valid JSON",
            details={"path": str(path), "reason": str(error)},
        ) from error
    if not isinstance(value, dict):
        raise OperatorApiError(
            invalid_code,
            f"{label.capitalize()} must be a JSON object",
            details={"path": str(path)},
        )
    return value


def _required_string(document: dict[str, Any], key: str, path: Path) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperatorApiError(
            OperatorErrorCode.OBSERVABILITY_CONFIG_INVALID,
            f"Observability field is missing or invalid: {key}",
            details={"path": str(path), "field": key},
        )
    return value


def _optional_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve(strict=False)


def _decode_payload(raw: Any) -> tuple[dict[str, Any], bool]:
    if raw is None or raw == "":
        return {}, False
    text = str(raw)
    if len(text) > MAX_EVENT_PAYLOAD_CHARS:
        return {
            "truncated": True,
            "raw_prefix": text[:MAX_EVENT_PAYLOAD_CHARS],
        }, True
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"invalid_json": True, "raw": text}, True
    if isinstance(value, dict):
        return value, False
    return {"value": value}, False


def _event_severity(event_type: str, payload: dict[str, Any], payload_warning: bool) -> str:
    lowered = event_type.lower()
    status = str(payload.get("status", "")).lower()
    if any(token in lowered for token in ("failed", "error", "collision", "rollback")):
        return "error"
    if status in {"failed", "timeout", "internal_error", "collision"}:
        return "error"
    if payload_warning or any(token in lowered for token in ("stale", "expired", "retry", "issue", "warning")):
        return "warning"
    return "info"


def _decode_command_summary(raw: Any) -> dict[str, Any]:
    try:
        command = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(command, dict):
        return {}
    payload = command.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    target_path = payload.get("path")
    if target_path is None:
        target_path = payload.get("target_path")
    return {
        "operation": command.get("operation"),
        "target_path": target_path if isinstance(target_path, str) else None,
        "profile_id": payload.get("profile_id") if isinstance(payload.get("profile_id"), str) else None,
    }
