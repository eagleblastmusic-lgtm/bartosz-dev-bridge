from __future__ import annotations

from bdb_operator.cli import _execute, _parser
from bdb_operator.models import OperatorResponse


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def events(self, root, **kwargs):
        self.calls.append(("events", {"root": root, **kwargs}))
        return OperatorResponse.success("events", data={"events": []})

    def current_operation(self, root):
        self.calls.append(("current_operation", {"root": root}))
        return OperatorResponse.success("current_operation", data={"active": False})

    def logs(self, root, **kwargs):
        self.calls.append(("logs", {"root": root, **kwargs}))
        return OperatorResponse.success("logs", data={"sources": []})


def test_events_cli_maps_all_filters() -> None:
    api = FakeApi()
    args = _parser().parse_args(
        [
            "events",
            "--root",
            "workspace",
            "--after-event-id",
            "17",
            "--limit",
            "25",
            "--session-id",
            "session-1",
            "--command-id",
            "command-1",
        ]
    )

    response = _execute(api, args)  # type: ignore[arg-type]

    assert response.ok is True
    assert api.calls == [
        (
            "events",
            {
                "root": "workspace",
                "after_event_id": 17,
                "limit": 25,
                "session_id": "session-1",
                "command_id": "command-1",
            },
        )
    ]


def test_current_operation_and_logs_cli_are_separate_reads() -> None:
    api = FakeApi()

    current_args = _parser().parse_args(["current-operation", "--root", "workspace"])
    logs_args = _parser().parse_args(
        ["logs", "--root", "workspace", "--max-bytes", "4096", "--max-lines", "30"]
    )

    current = _execute(api, current_args)  # type: ignore[arg-type]
    logs = _execute(api, logs_args)  # type: ignore[arg-type]

    assert current.ok is True and logs.ok is True
    assert api.calls == [
        ("current_operation", {"root": "workspace"}),
        ("logs", {"root": "workspace", "max_bytes": 4096, "max_lines": 30}),
    ]
