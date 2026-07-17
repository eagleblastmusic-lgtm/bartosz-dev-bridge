from __future__ import annotations

import json
from typing import Any, Callable, Type

from .models import BridgeErrorCode, CommandState, ResultCoordinationOutcome, StagedResult
from .protocol import BridgeError
from .recovery_journal import sha256_bytes


_MAX_FINAL_CONTENT_CHARS = 8_000


def _candidate_value(value: dict[str, Any], content: str) -> dict[str, Any]:
    original_content = value.get("content")
    encoded = content.encode("utf-8", errors="strict")
    candidate = dict(value)
    candidate["content"] = content
    candidate["content_sha256"] = sha256_bytes(encoded)
    candidate["returned_bytes"] = len(encoded)
    candidate["truncated"] = bool(value.get("truncated", False)) or content != original_content
    return candidate


def _staged_preserves_candidate(staged: StagedResult, candidate: dict[str, Any]) -> bool:
    try:
        parsed = json.loads(staged.result_json)
    except (json.JSONDecodeError, UnicodeError):
        return False
    data = parsed.get("data")
    if not isinstance(data, dict):
        return False
    content = candidate["content"]
    return (
        staged.result_bytes == staged.result_json.encode("utf-8", errors="strict")
        and data.get("content") == content
        and data.get("content_sha256") == candidate["content_sha256"]
        and data.get("returned_bytes") == candidate["returned_bytes"]
        and data.get("returned_bytes") == len(content.encode("utf-8", errors="strict"))
    )


def _fit_open_read_result(
    original_build: Callable[..., StagedResult],
    value: dict[str, Any],
    *,
    session: Any,
    command: Any,
    started_at: str,
    finished_at: str,
) -> StagedResult:
    original_content = value.get("content")
    if not isinstance(original_content, str):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "open_read content must be a string")

    low = 0
    high = min(len(original_content), _MAX_FINAL_CONTENT_CHARS)
    best: StagedResult | None = None

    while low <= high:
        midpoint = (low + high) // 2
        candidate = _candidate_value(value, original_content[:midpoint])
        try:
            staged = original_build(
                candidate,
                session=session,
                command=command,
                started_at=started_at,
                finished_at=finished_at,
            )
        except BridgeError as exc:
            if str(exc.code) != BridgeErrorCode.RESULT_TOO_LARGE.value:
                raise
            high = midpoint - 1
            continue

        if _staged_preserves_candidate(staged, candidate):
            best = staged
            low = midpoint + 1
        else:
            high = midpoint - 1

    if best is None:
        raise BridgeError(
            BridgeErrorCode.RESULT_TOO_LARGE,
            "Unable to fit a self-consistent open_read result into the result envelope",
        )
    return best


def _fail_closed_open_read(journal: Any, command_id: str, exc: BridgeError):
    current = journal.get_command(command_id)
    if current is None or current.state not in {CommandState.CLAIMED, CommandState.EXECUTING}:
        return None
    journal.mark_workspace_recovery_blocked(
        session_id=current.session_id,
        command_id=command_id,
        reason_code=str(exc.code),
        diagnostic={
            "reason": str(exc)[:200],
            "error_code": str(exc.code),
            "operation": "open_read",
        },
    )
    return journal.get_command(command_id)


def install_open_read_result_safety(result_coordinator_cls: Type[object]) -> None:
    from . import open_read_result as open_read_module

    if getattr(open_read_module, "_open_read_result_safety_installed", False):
        return

    original_build = open_read_module.build_open_read_result
    original_process = result_coordinator_cls.process

    def safe_build(
        value: dict[str, Any],
        *,
        session: Any,
        command: Any,
        started_at: str,
        finished_at: str,
    ) -> StagedResult:
        return _fit_open_read_result(
            original_build,
            value,
            session=session,
            command=command,
            started_at=started_at,
            finished_at=finished_at,
        )

    def guarded_process(self: Any, command_id: str) -> ResultCoordinationOutcome:
        command = self.journal.get_command(command_id)
        is_open_read = (
            command is not None
            and open_read_module._operation(command.command_json) == open_read_module.OPEN_READ_OPERATION
        )
        try:
            return original_process(self, command_id)
        except BridgeError as exc:
            if not is_open_read:
                raise
            updated = _fail_closed_open_read(self.journal, command_id, exc)
            if updated is None:
                raise
            return ResultCoordinationOutcome(command_id, updated.state, staged=False)

    open_read_module.build_open_read_result = safe_build
    result_coordinator_cls.process = guarded_process
    setattr(open_read_module, "_open_read_result_safety_installed", True)
