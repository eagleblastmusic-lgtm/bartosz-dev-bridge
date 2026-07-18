from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bdb_operator import OperatorApi, OperatorResponse


GUI_HISTORY_SCHEMA = "bdb-gui-history-v1"
GUI_EVENT_SCHEMA = "bdb-gui-event-v1"
DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 500


class HistoryOperator(Protocol):
    def events(
        self,
        workspace_root: str | Path,
        *,
        after_event_id: int = 0,
        limit: int = 100,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> OperatorResponse:
        ...


@dataclass(frozen=True)
class GuiEvent:
    event_id: str
    sequence: int
    event_type: str
    occurred_at: str
    source: str
    severity: str
    correlation_id: str | None
    session_id: str | None
    command_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    schema: str = GUI_EVENT_SCHEMA

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "GuiEvent":
        if document.get("schema") != "bdb-event-v1":
            raise ValueError("Operator event schema is unsupported")
        payload = document.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Operator event payload must be an object")
        return cls(
            event_id=_required_string(document, "event_id"),
            sequence=_required_non_negative_int(document, "sequence"),
            event_type=_required_string(document, "event_type"),
            occurred_at=_required_string(document, "occurred_at"),
            source=_required_string(document, "source"),
            severity=_required_string(document, "severity"),
            correlation_id=_optional_string(document.get("correlation_id"), "correlation_id"),
            session_id=_optional_string(document.get("session_id"), "session_id"),
            command_id=_optional_string(document.get("command_id"), "command_id"),
            payload=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "severity": self.severity,
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "command_id": self.command_id,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class HistoryCursor:
    after_event_id: int
    next_after_event_id: int
    has_more: bool

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "HistoryCursor":
        return cls(
            after_event_id=_required_non_negative_int(document, "after_event_id"),
            next_after_event_id=_required_non_negative_int(document, "next_after_event_id"),
            has_more=_required_bool(document, "has_more"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "after_event_id": self.after_event_id,
            "next_after_event_id": self.next_after_event_id,
            "has_more": self.has_more,
        }


@dataclass(frozen=True)
class HistoryFilters:
    session_id: str | None
    command_id: str | None

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "HistoryFilters":
        return cls(
            session_id=_optional_string(document.get("session_id"), "session_id"),
            command_id=_optional_string(document.get("command_id"), "command_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "command_id": self.command_id}


@dataclass(frozen=True)
class HistorySnapshot:
    workspace_root: str
    project_alias: str | None
    events: tuple[GuiEvent, ...]
    cursor: HistoryCursor
    filters: HistoryFilters
    operator_operation_id: str
    error_code: str | None = None
    error_message: str | None = None
    read_only: bool = True
    mutation_operations_invoked: int = 0
    schema: str = GUI_HISTORY_SCHEMA

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @classmethod
    def failure(
        cls,
        workspace_root: str | Path,
        *,
        operation_id: str,
        project_alias: str | None,
        error_code: str,
        error_message: str,
        after_event_id: int = 0,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> "HistorySnapshot":
        return cls(
            workspace_root=_resolved(workspace_root),
            project_alias=project_alias,
            events=(),
            cursor=HistoryCursor(after_event_id, after_event_id, False),
            filters=HistoryFilters(session_id, command_id),
            operator_operation_id=operation_id,
            error_code=error_code,
            error_message=error_message,
        )

    @classmethod
    def from_response(
        cls,
        workspace_root: str | Path,
        response: OperatorResponse,
        *,
        requested_after_event_id: int,
        requested_session_id: str | None,
        requested_command_id: str | None,
    ) -> "HistorySnapshot":
        if not response.ok:
            return cls.failure(
                workspace_root,
                operation_id=response.operation_id,
                project_alias=response.project_alias,
                error_code=response.error.code if response.error is not None else "operator_error",
                error_message=(
                    response.error.message if response.error is not None else "History read failed"
                ),
                after_event_id=requested_after_event_id,
                session_id=requested_session_id,
                command_id=requested_command_id,
            )
        try:
            data = _object(response.data, "history data")
            raw_events = data.get("events")
            if not isinstance(raw_events, list):
                raise ValueError("Operator events must be an array")
            events = tuple(GuiEvent.from_document(_object(item, "event")) for item in raw_events)
            cursor = HistoryCursor.from_document(_object(data.get("cursor"), "cursor"))
            filters = HistoryFilters.from_document(_object(data.get("filters"), "filters"))
            if cursor.after_event_id != requested_after_event_id:
                raise ValueError("Operator history cursor does not match requested cursor")
            if filters.session_id != requested_session_id or filters.command_id != requested_command_id:
                raise ValueError("Operator history filters do not match requested filters")
            previous = requested_after_event_id
            for event in events:
                if event.sequence <= previous:
                    raise ValueError("Operator events must be strictly ordered by sequence")
                previous = event.sequence
            if events and cursor.next_after_event_id != events[-1].sequence:
                raise ValueError("Operator next cursor does not match the last event")
            if not events and cursor.next_after_event_id != requested_after_event_id:
                raise ValueError("Empty history page must preserve the requested cursor")
            return cls(
                workspace_root=_resolved(workspace_root),
                project_alias=_optional_string(data.get("project_alias"), "project_alias")
                or response.project_alias,
                events=events,
                cursor=cursor,
                filters=filters,
                operator_operation_id=response.operation_id,
            )
        except ValueError as error:
            return cls.failure(
                workspace_root,
                operation_id=response.operation_id,
                project_alias=response.project_alias,
                error_code="invalid_operator_response",
                error_message=str(error),
                after_event_id=requested_after_event_id,
                session_id=requested_session_id,
                command_id=requested_command_id,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workspace_root": self.workspace_root,
            "project_alias": self.project_alias,
            "ok": self.ok,
            "read_only": self.read_only,
            "mutation_operations_invoked": self.mutation_operations_invoked,
            "operator_operation_id": self.operator_operation_id,
            "events": [event.to_dict() for event in self.events],
            "cursor": self.cursor.to_dict(),
            "filters": self.filters.to_dict(),
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }


class HistoryService:
    """Bounded read-only GUI adapter over OperatorApi.events()."""

    def __init__(self, operator: HistoryOperator | None = None) -> None:
        self._operator = operator or OperatorApi()

    def read(
        self,
        workspace_root: str | Path,
        *,
        after_event_id: int = 0,
        limit: int = DEFAULT_HISTORY_LIMIT,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> HistorySnapshot:
        _validate_query(after_event_id, limit, session_id, command_id)
        response = self._operator.events(
            workspace_root,
            after_event_id=after_event_id,
            limit=limit,
            session_id=session_id,
            command_id=command_id,
        )
        return HistorySnapshot.from_response(
            workspace_root,
            response,
            requested_after_event_id=after_event_id,
            requested_session_id=session_id,
            requested_command_id=command_id,
        )


def _validate_query(
    after_event_id: int,
    limit: int,
    session_id: str | None,
    command_id: str | None,
) -> None:
    if isinstance(after_event_id, bool) or not isinstance(after_event_id, int) or after_event_id < 0:
        raise ValueError("after_event_id must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_HISTORY_LIMIT}")
    for name, value in (("session_id", session_id), ("command_id", command_id)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{name} must be a non-empty string when provided")


def _resolved(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Operator {label} must be an object")
    return value


def _required_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value


def _optional_string(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Operator field has an invalid string type: {key}")
    return value


def _required_non_negative_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value


def _required_bool(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value
