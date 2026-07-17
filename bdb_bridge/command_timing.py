from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .journal import Journal
from .models import CommandState
from .protocol import parse_strict_utc_timestamp


def _first_event_times(journal: Journal, command_id: str) -> tuple[dict[str, str], dict[str, str]]:
    event_times: dict[str, str] = {}
    state_times: dict[str, str] = {}

    for event in journal.list_events(command_id=command_id):
        event_times.setdefault(event.event_type, event.created_at)
        if event.event_type != "command.state_changed" or event.payload_json is None:
            continue
        try:
            payload = json.loads(event.payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        to_state = payload.get("to_state") if isinstance(payload, dict) else None
        if isinstance(to_state, str):
            state_times.setdefault(to_state, event.created_at)

    return event_times, state_times


def _result_execution_times(result_json: str | None) -> tuple[str | None, str | None]:
    if result_json is None:
        return None, None
    try:
        payload = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    started_at = payload.get("started_at")
    finished_at = payload.get("finished_at")
    return (
        started_at if isinstance(started_at, str) else None,
        finished_at if isinstance(finished_at, str) else None,
    )


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_strict_utc_timestamp(value, field="timing")
    except Exception:
        return None


def _duration_ms(start: str | None, finish: str | None) -> float | None:
    start_dt = _parse(start)
    finish_dt = _parse(finish)
    if start_dt is None or finish_dt is None:
        return None
    return round((finish_dt - start_dt).total_seconds() * 1000.0, 3)


def build_command_timing(journal: Journal, command_id: str) -> dict[str, Any]:
    ingestion = journal.get_command_ingestion(command_id)
    result = journal.get_result(command_id)
    outbox = journal.get_outbox(command_id)
    event_times, state_times = _first_event_times(journal, command_id)
    result_started_at, result_finished_at = _result_execution_times(
        result.result_json if result is not None else None
    )

    remote_created_at = ingestion.created_remote_at if ingestion is not None else None
    first_seen_at = ingestion.first_seen_at if ingestion is not None else event_times.get("command.discovered")
    validated_at = event_times.get("command.validated")
    claimed_at = event_times.get("command.claimed") or state_times.get(CommandState.CLAIMED.value)
    execution_started_at = result_started_at or state_times.get(CommandState.EXECUTING.value)
    execution_finished_at = result_finished_at or state_times.get(CommandState.EFFECT_RECORDED.value)
    result_staged_at = result.created_at if result is not None else event_times.get("result.staged")
    result_published_at = outbox.published_at if outbox is not None else event_times.get("result.published")

    timestamps = {
        "remote_created_at": remote_created_at,
        "first_seen_at": first_seen_at,
        "validated_at": validated_at,
        "claimed_at": claimed_at,
        "execution_started_at": execution_started_at,
        "execution_finished_at": execution_finished_at,
        "result_staged_at": result_staged_at,
        "result_published_at": result_published_at,
    }
    durations = {
        "inbound_transport_ms": _duration_ms(remote_created_at, first_seen_at),
        "validation_ms": _duration_ms(first_seen_at, validated_at),
        "scheduler_queue_ms": _duration_ms(validated_at, claimed_at),
        "pre_execution_ms": _duration_ms(claimed_at, execution_started_at),
        "execution_ms": _duration_ms(execution_started_at, execution_finished_at),
        "result_staging_ms": _duration_ms(execution_finished_at, result_staged_at),
        "result_publication_ms": _duration_ms(result_staged_at, result_published_at),
        "end_to_end_ms": _duration_ms(remote_created_at, result_published_at),
    }

    return {
        "timestamps": timestamps,
        "durations_ms": durations,
    }
