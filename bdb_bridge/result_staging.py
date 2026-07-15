from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .journal import Journal
from .models import (
    BridgeErrorCode,
    CommandRecord,
    CommandState,
    ExecutionOutcome,
    OperationEffectRecord,
    OperationPlanRecord,
    OutboxRecord,
    ResultRecord,
    ResultStatus,
    SessionRecord,
    StagedResult,
)
from .protocol import BridgeError, SCHEMA_VERSION, parse_strict_utc_timestamp, result_path_for
from .recovery_journal import sha256_bytes
from .serializers import MAX_RESULT_BYTES, finalize_result

EXECUTOR_VERSION = "0.5.0-ghb0"
_ALLOWED = frozenset(member.value for member in ResultStatus)
FaultHook = Callable[[str], None]


def _strict_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field} must be a string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field} must be strict UTF-8") from exc
    return value


def _duration_ms(started_at: str, finished_at: str) -> int:
    started = parse_strict_utc_timestamp(started_at, field="started_at")
    finished = parse_strict_utc_timestamp(finished_at, field="finished_at")
    if finished < started:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "finished_at cannot be before started_at")
    return int((finished - started).total_seconds() * 1000)


def _validate_inputs(
    session: SessionRecord,
    command: CommandRecord,
    plan: OperationPlanRecord,
    effect: OperationEffectRecord,
    outcome: ExecutionOutcome,
) -> None:
    if command.state != CommandState.EFFECT_RECORDED:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Result build requires EFFECT_RECORDED, got {command.state.value}",
        )
    if command.session_id != session.session_id:
        raise BridgeError(BridgeErrorCode.SESSION_MISMATCH, "Command/session mismatch")
    if plan.command_id != command.command_id or effect.command_id != command.command_id:
        raise BridgeError(BridgeErrorCode.COMMAND_ID_MISMATCH, "Plan/effect command mismatch")
    if plan.session_id != session.session_id or effect.session_id != session.session_id:
        raise BridgeError(BridgeErrorCode.SESSION_MISMATCH, "Plan/effect session mismatch")
    if effect.plan_sha256 != plan.plan_sha256:
        raise BridgeError(BridgeErrorCode.EFFECT_COLLISION, "Effect does not reference persisted plan")
    expected = (
        outcome.workspace_revision_before == effect.workspace_revision_before,
        outcome.workspace_revision_after == effect.workspace_revision_after,
        outcome.workspace_state_hash_before == effect.workspace_state_hash_before,
        outcome.workspace_state_hash_after == effect.workspace_state_hash_after,
    )
    if not all(expected):
        raise BridgeError(BridgeErrorCode.STATE_MISMATCH, "Execution outcome does not match persisted effect")
    if outcome.status not in _ALLOWED:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"Unsupported result status: {outcome.status!r}")


@dataclass(frozen=True)
class ResultBuildInput:
    session: SessionRecord
    command: CommandRecord
    plan: OperationPlanRecord
    effect: OperationEffectRecord
    outcome: ExecutionOutcome
    started_at: str
    finished_at: str


class ResultStager:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    def build(self, value: ResultBuildInput) -> StagedResult:
        _validate_inputs(value.session, value.command, value.plan, value.effect, value.outcome)
        duration_ms = _duration_ms(value.started_at, value.finished_at)
        profile = value.outcome.profile_run
        stdout = _strict_text(profile.stdout if profile else "", "stdout")
        stderr = _strict_text(profile.stderr if profile else "", "stderr")
        diff = _strict_text(value.outcome.diff, "diff")
        summary = _strict_text(value.outcome.summary, "summary")
        if not isinstance(value.outcome.changed_files, list) or not all(isinstance(item, str) for item in value.outcome.changed_files):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "changed_files must be a string list")
        changed_files = sorted(set(value.outcome.changed_files))
        for item in changed_files:
            _strict_text(item, "changed_files item")

        error_code: str | None
        if value.outcome.error_code is None:
            error_code = None
        else:
            error_code = _strict_text(str(value.outcome.error_code), "error_code")

        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": value.session.session_id,
            "command_id": value.command.command_id,
            "sequence": value.command.sequence,
            "started_at": value.started_at,
            "finished_at": value.finished_at,
            "duration_ms": duration_ms,
            "executor_version": EXECUTOR_VERSION,
            "command_commit_sha": value.command.command_commit_sha,
            "workspace_revision_before": value.effect.workspace_revision_before,
            "workspace_revision_after": value.effect.workspace_revision_after,
            "state_hash_before": value.effect.workspace_state_hash_before,
            "state_hash_after": value.effect.workspace_state_hash_after,
            "status": value.outcome.status,
            "error_code": error_code,
            "exit_code": profile.exit_code if profile else None,
            "summary": summary,
            "stdout_tail": stdout,
            "stderr_tail": stderr,
            "stdout_sha256": sha256_bytes(stdout.encode("utf-8", errors="strict")),
            "stderr_sha256": sha256_bytes(stderr.encode("utf-8", errors="strict")),
            "changed_files": changed_files,
            "diff": diff,
            "diff_sha256": sha256_bytes(diff.encode("utf-8", errors="strict")),
            "artifacts": [],
            "truncated": False,
        }
        try:
            result_json = finalize_result(result)
            result_bytes = result_json.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Final result must be strict UTF-8") from exc
        if len(result_bytes) > MAX_RESULT_BYTES:
            raise BridgeError(BridgeErrorCode.RESULT_TOO_LARGE, "Finalized result exceeds 16 KiB")
        return StagedResult(
            command_id=value.command.command_id,
            result_json=result_json,
            result_bytes=result_bytes,
            result_sha256=sha256_bytes(result_bytes),
            remote_path=result_path_for(value.session.session_id, value.command.sequence),
        )

    def stage(
        self,
        value: ResultBuildInput,
        *,
        fault_hook: FaultHook | None = None,
    ) -> tuple[StagedResult, ResultRecord, OutboxRecord]:
        staged = self.build(value)
        result, outbox = self.journal.stage_result_and_enqueue(
            command_id=staged.command_id,
            result_json=staged.result_json,
            remote_path=staged.remote_path,
            fault_hook=fault_hook,
        )
        if result.result_sha256 != staged.result_sha256 or outbox.result_sha256 != staged.result_sha256:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Persisted result/outbox hash differs from staged bytes")
        return staged, result, outbox
