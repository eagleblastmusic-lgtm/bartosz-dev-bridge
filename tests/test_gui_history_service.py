from __future__ import annotations

from pathlib import Path

import pytest

from bdb_gui.history import GUI_EVENT_SCHEMA, GUI_HISTORY_SCHEMA, HistoryService
from bdb_operator.models import OperatorError, OperatorResponse


class FakeHistoryOperator:
    def __init__(self, response: OperatorResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def events(
        self,
        workspace_root: str | Path,
        *,
        after_event_id: int = 0,
        limit: int = 100,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> OperatorResponse:
        self.calls.append(
            {
                "workspace_root": str(workspace_root),
                "after_event_id": after_event_id,
                "limit": limit,
                "session_id": session_id,
                "command_id": command_id,
            }
        )
        return self.response


def event(sequence: int, *, session_id: str = "session-1") -> dict[str, object]:
    return {
        "schema": "bdb-event-v1",
        "event_id": f"journal:alpha:{sequence}",
        "sequence": sequence,
        "event_type": "COMMAND_STATE_CHANGED",
        "occurred_at": f"2026-07-18T21:00:{sequence:02d}Z",
        "source": "bridge",
        "severity": "info",
        "correlation_id": f"session-1:{sequence:06d}",
        "session_id": session_id,
        "command_id": f"session-1:{sequence:06d}",
        "payload": {"state": "executing", "sequence": sequence},
    }


def page_response(
    events: list[dict[str, object]],
    *,
    after_event_id: int,
    next_after_event_id: int,
    has_more: bool,
    session_id: str | None = None,
    command_id: str | None = None,
) -> OperatorResponse:
    return OperatorResponse.success(
        "events",
        project_alias="alpha",
        operation_id="history-op",
        data={
            "project_alias": "alpha",
            "events": events,
            "cursor": {
                "after_event_id": after_event_id,
                "next_after_event_id": next_after_event_id,
                "has_more": has_more,
            },
            "filters": {"session_id": session_id, "command_id": command_id},
        },
    )


def test_history_service_preserves_bounded_query_and_event_contract(tmp_path: Path) -> None:
    response = page_response(
        [event(11), event(12)],
        after_event_id=10,
        next_after_event_id=12,
        has_more=True,
        session_id="session-1",
    )
    operator = FakeHistoryOperator(response)
    service = HistoryService(operator)

    snapshot = service.read(
        tmp_path / "alpha",
        after_event_id=10,
        limit=2,
        session_id="session-1",
    )

    assert snapshot.schema == GUI_HISTORY_SCHEMA
    assert snapshot.ok is True
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0
    assert [item.sequence for item in snapshot.events] == [11, 12]
    assert all(item.schema == GUI_EVENT_SCHEMA for item in snapshot.events)
    assert snapshot.events[0].payload == {"state": "executing", "sequence": 11}
    assert snapshot.cursor.after_event_id == 10
    assert snapshot.cursor.next_after_event_id == 12
    assert snapshot.cursor.has_more is True
    assert snapshot.filters.session_id == "session-1"
    assert operator.calls == [
        {
            "workspace_root": str(tmp_path / "alpha"),
            "after_event_id": 10,
            "limit": 2,
            "session_id": "session-1",
            "command_id": None,
        }
    ]
    document = snapshot.to_dict()
    assert document["events"][0]["schema"] == GUI_EVENT_SCHEMA
    assert document["error"] is None


def test_empty_page_preserves_cursor(tmp_path: Path) -> None:
    response = page_response(
        [],
        after_event_id=20,
        next_after_event_id=20,
        has_more=False,
    )
    snapshot = HistoryService(FakeHistoryOperator(response)).read(
        tmp_path / "alpha", after_event_id=20
    )

    assert snapshot.ok is True
    assert snapshot.events == ()
    assert snapshot.cursor.next_after_event_id == 20
    assert snapshot.cursor.has_more is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"after_event_id": -1}, "after_event_id"),
        ({"after_event_id": True}, "after_event_id"),
        ({"limit": 0}, "limit"),
        ({"limit": 501}, "limit"),
        ({"limit": True}, "limit"),
        ({"session_id": ""}, "session_id"),
        ({"command_id": "   "}, "command_id"),
    ],
)
def test_query_validation_happens_before_operator_call(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    operator = FakeHistoryOperator(
        page_response([], after_event_id=0, next_after_event_id=0, has_more=False)
    )
    service = HistoryService(operator)

    with pytest.raises(ValueError, match=message):
        service.read(tmp_path / "alpha", **kwargs)  # type: ignore[arg-type]

    assert operator.calls == []


def test_operator_failure_is_typed_and_read_only(tmp_path: Path) -> None:
    response = OperatorResponse.failure(
        "events",
        project_alias="alpha",
        operation_id="history-failed",
        error=OperatorError(code="journal_unavailable", message="Journal is locked"),
    )
    snapshot = HistoryService(FakeHistoryOperator(response)).read(tmp_path / "alpha")

    assert snapshot.ok is False
    assert snapshot.error_code == "journal_unavailable"
    assert snapshot.error_message == "Journal is locked"
    assert snapshot.events == ()
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0


@pytest.mark.parametrize(
    "response",
    [
        page_response([event(12), event(11)], after_event_id=10, next_after_event_id=11, has_more=False),
        page_response([event(11)], after_event_id=10, next_after_event_id=99, has_more=False),
        page_response([], after_event_id=10, next_after_event_id=11, has_more=False),
        page_response([event(11)], after_event_id=9, next_after_event_id=11, has_more=False),
        page_response([event(11)], after_event_id=10, next_after_event_id=11, has_more=False, session_id="other"),
    ],
)
def test_invalid_cursor_order_or_filters_becomes_typed_error(
    tmp_path: Path,
    response: OperatorResponse,
) -> None:
    snapshot = HistoryService(FakeHistoryOperator(response)).read(
        tmp_path / "alpha",
        after_event_id=10,
    )

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
