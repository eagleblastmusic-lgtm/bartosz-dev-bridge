from __future__ import annotations

import json
from typing import Any, Type

from .models import (
    BridgeErrorCode,
    CommandState,
    ResultCoordinationOutcome,
    ResultStatus,
    StagedResult,
)
from .multi_file_patch_gate import MULTI_FILE_PATCH_OPERATION
from .multi_file_patch_recovery_models import MultiFileCheckpointState
from .multi_file_patch_runtime import MultiFilePatchRuntimeCoordinator
from .multi_file_patch_runtime_models import MultiFilePatchRuntimeResult
from .protocol import (
    BridgeError,
    SCHEMA_VERSION,
    parse_strict_utc_timestamp,
    result_path_for,
    validate_repo_relative_path,
    validate_strict_utc_timestamp,
)
from .recovery_journal import sha256_bytes
from .serializers import MAX_RESULT_BYTES, canonical_json, finalize_result


EXECUTOR_VERSION = "0.6.0-ghb2d"
_ALLOWED_STATUSES = frozenset(member.value for member in ResultStatus)


def _operation(command_json: str) -> str | None:
    try:
        value = json.loads(command_json)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return value.get("operation") if isinstance(value, dict) else None


def _duration_ms(started_at: str, finished_at: str) -> int:
    started = parse_strict_utc_timestamp(started_at, field="started_at")
    finished = parse_strict_utc_timestamp(finished_at, field="finished_at")
    if finished < started:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "finished_at cannot precede started_at")
    return int((finished - started).total_seconds() * 1000)


def build_multi_file_patch_result(
    value: MultiFilePatchRuntimeResult,
    *,
    session: Any,
    command: Any,
) -> StagedResult:
    checkpoint = value.checkpoint.record
    profile = value.profile
    outcome = value.outcome
    success = profile.status == ResultStatus.SUCCESS.value
    expected_checkpoint = (
        MultiFileCheckpointState.COMMITTED
        if success
        else MultiFileCheckpointState.ROLLED_BACK
    )
    if checkpoint.state is not expected_checkpoint:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CONFLICT,
            "Profile/checkpoint terminal state mismatch before result build",
        )
    attempted_files = [item.path for item in value.checkpoint.paths]
    expected_changed = attempted_files if success else []
    if outcome.changed_files != expected_changed:
        raise BridgeError(
            BridgeErrorCode.STATE_MISMATCH,
            "Runtime changed_files does not match terminal checkpoint",
        )
    stdout = profile.stdout_tail
    stderr = profile.stderr_tail
    diff = outcome.diff
    truncated = (
        profile.stdout_sha256 != sha256_bytes(stdout.encode("utf-8", errors="strict"))
        or profile.stderr_sha256 != sha256_bytes(stderr.encode("utf-8", errors="strict"))
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session.session_id,
        "command_id": command.command_id,
        "sequence": command.sequence,
        "started_at": profile.started_at,
        "finished_at": profile.finished_at,
        "duration_ms": _duration_ms(profile.started_at, profile.finished_at),
        "executor_version": EXECUTOR_VERSION,
        "command_commit_sha": command.command_commit_sha,
        "workspace_revision_before": outcome.workspace_revision_before,
        "workspace_revision_after": outcome.workspace_revision_after,
        "state_hash_before": outcome.workspace_state_hash_before,
        "state_hash_after": outcome.workspace_state_hash_after,
        "status": outcome.status,
        "error_code": outcome.error_code,
        "exit_code": profile.exit_code,
        "summary": outcome.summary,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "stdout_sha256": profile.stdout_sha256,
        "stderr_sha256": profile.stderr_sha256,
        "changed_files": expected_changed,
        "diff": diff,
        "diff_sha256": sha256_bytes(diff.encode("utf-8", errors="strict")),
        "artifacts": [],
        "data": {
            "operation": MULTI_FILE_PATCH_OPERATION,
            "patch_sha256": checkpoint.patch_sha256,
            "plan_sha256": checkpoint.plan_sha256,
            "checkpoint_sha256": checkpoint.checkpoint_sha256,
            "checkpoint_state": checkpoint.state.value,
            "attempted_files": attempted_files,
            "rollback_performed": not success,
            "profile_duration_ms": profile.duration_ms,
        },
        "truncated": truncated,
    }
    result_json = finalize_result(result)
    result_bytes = result_json.encode("utf-8", errors="strict")
    if len(result_bytes) > MAX_RESULT_BYTES:
        raise BridgeError(BridgeErrorCode.RESULT_TOO_LARGE, "Final result exceeds 16 KiB")
    return StagedResult(
        command_id=command.command_id,
        result_json=result_json,
        result_bytes=result_bytes,
        result_sha256=sha256_bytes(result_bytes),
        remote_path=result_path_for(session.session_id, command.sequence),
    )


def _validate_multi_file_staged_result(
    journal: Any,
    *,
    command_id: str,
    result_json: str,
    remote_path: str,
):
    from . import outbox_common as common

    if not isinstance(result_json, str) or not result_json:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result_json must be non-empty")
    try:
        result_bytes = result_json.encode("utf-8", errors="strict")
        parsed = json.loads(result_json)
    except (UnicodeEncodeError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid strict UTF-8 result JSON") from exc
    if len(result_bytes) > MAX_RESULT_BYTES or not isinstance(parsed, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid bounded result JSON")
    command = journal.get_command(command_id)
    if command is None:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Command not found")
    session = journal.get_session(command.session_id)
    bundle = journal.get_multi_file_patch_bundle(command_id)
    profile = journal.get_multi_file_patch_profile_run(command_id)
    if session is None or bundle is None or profile is None:
        raise BridgeError(
            BridgeErrorCode.JOURNAL_CONFLICT,
            "Session, checkpoint and profile outcome must exist before staging",
        )
    if command.state not in {
        CommandState.EFFECT_RECORDED,
        CommandState.RESULT_STAGED,
        CommandState.RESULT_PUBLISHED,
    }:
        raise BridgeError(
            BridgeErrorCode.INVALID_STATE_TRANSITION,
            f"Multi-file staging cannot use {command.state.value}",
        )
    success = profile.status == ResultStatus.SUCCESS.value
    expected_state = (
        MultiFileCheckpointState.COMMITTED
        if success
        else MultiFileCheckpointState.ROLLED_BACK
    )
    if bundle.record.state is not expected_state:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Profile/checkpoint state mismatch")
    revision_after = (
        bundle.record.workspace_revision_after
        if success
        else bundle.record.workspace_revision_before
    )
    state_after = (
        bundle.record.workspace_state_hash_after
        if success
        else bundle.record.workspace_state_hash_before
    )
    expected_fields = {
        "schema_version": SCHEMA_VERSION,
        "session_id": command.session_id,
        "command_id": command.command_id,
        "sequence": command.sequence,
        "command_commit_sha": command.command_commit_sha,
        "workspace_revision_before": bundle.record.workspace_revision_before,
        "workspace_revision_after": revision_after,
        "state_hash_before": bundle.record.workspace_state_hash_before,
        "state_hash_after": state_after,
        "status": profile.status,
        "error_code": None if success else profile.status,
        "exit_code": profile.exit_code,
    }
    for field, expected in expected_fields.items():
        if parsed.get(field) != expected:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"result {field} does not match durable multi-file outcome",
            )
    attempted = [item.path for item in bundle.paths]
    expected_changed = attempted if success else []
    if parsed.get("changed_files") != expected_changed:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid multi-file changed_files")
    data = parsed.get("data")
    expected_data = {
        "operation": MULTI_FILE_PATCH_OPERATION,
        "patch_sha256": bundle.record.patch_sha256,
        "plan_sha256": bundle.record.plan_sha256,
        "checkpoint_sha256": bundle.record.checkpoint_sha256,
        "checkpoint_state": bundle.record.state.value,
        "attempted_files": attempted,
        "rollback_performed": not success,
        "profile_duration_ms": profile.duration_ms,
    }
    if data != expected_data:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid multi-file result data")
    if parsed.get("artifacts") != []:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "artifacts must be empty")
    for field in ("started_at", "finished_at"):
        validate_strict_utc_timestamp(parsed.get(field), field=f"result.{field}")
    if parsed.get("started_at") != profile.started_at or parsed.get("finished_at") != profile.finished_at:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Result timestamps differ from profile")
    if parsed.get("duration_ms") != _duration_ms(profile.started_at, profile.finished_at):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Result duration differs from timestamps")
    if parsed.get("stdout_sha256") != profile.stdout_sha256:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "stdout hash differs from profile")
    if parsed.get("stderr_sha256") != profile.stderr_sha256:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "stderr hash differs from profile")
    for text_field in ("stdout_tail", "stderr_tail", "diff", "summary"):
        value = parsed.get(text_field)
        if not isinstance(value, str):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{text_field} must be a string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{text_field} is not strict UTF-8") from exc
    common._require_hash(parsed.get("diff_sha256"), "result.diff_sha256")
    if not parsed.get("truncated", False) and parsed["diff_sha256"] != sha256_bytes(
        parsed["diff"].encode("utf-8", errors="strict")
    ):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "diff hash mismatch")
    status = parsed.get("status")
    if status not in _ALLOWED_STATUSES:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Unsupported status")
    error_code = parsed.get("error_code")
    if success and error_code is not None:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Successful result has error_code")
    common._validate_end_marker(parsed)
    expected_path = result_path_for(command.session_id, command.sequence)
    validate_repo_relative_path(remote_path)
    if remote_path != expected_path:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid result remote path")
    return parsed, result_bytes, status, error_code, command, bundle, profile


def install_multi_file_patch_result_support(
    result_coordinator_cls: Type[object],
) -> None:
    from . import outbox_common
    from . import outbox_staging

    if getattr(result_coordinator_cls, "_ghb2d_result_installed", False):
        return
    original_init = result_coordinator_cls.__init__
    original_process = result_coordinator_cls.process
    original_validator = outbox_common._validate_staged_result

    def validator(journal: Any, *, command_id: str, result_json: str, remote_path: str):
        command = journal.get_command(command_id)
        if command is not None and _operation(command.command_json) == MULTI_FILE_PATCH_OPERATION:
            return _validate_multi_file_staged_result(
                journal,
                command_id=command_id,
                result_json=result_json,
                remote_path=remote_path,
            )
        return original_validator(
            journal,
            command_id=command_id,
            result_json=result_json,
            remote_path=remote_path,
        )

    def patched_init(
        self: Any,
        config: Any,
        journal: Any,
        outbox_processor: Any,
        *,
        now_fn: Any = None,
        fault_hook: Any = None,
        execution_factory: Any = None,
        instance_lock: Any = None,
        multi_file_runtime_factory: Any = None,
    ) -> None:
        original_init(
            self,
            config,
            journal,
            outbox_processor,
            now_fn=now_fn,
            fault_hook=fault_hook,
            execution_factory=execution_factory,
        )
        self.instance_lock = instance_lock
        self.multi_file_runtime_factory = multi_file_runtime_factory

    def patched_process(self: Any, command_id: str) -> ResultCoordinationOutcome:
        command = self.journal.get_command(command_id)
        if command is None or _operation(command.command_json) != MULTI_FILE_PATCH_OPERATION:
            return original_process(self, command_id)
        if command.state in {CommandState.RESULT_STAGED, CommandState.RESULT_PUBLISHED}:
            return original_process(self, command_id)
        if self.instance_lock is None:
            raise BridgeError(
                BridgeErrorCode.INSTANCE_LOCK_FAILED,
                "Multi-file runtime requires the active Bridge instance lock",
            )
        started_at = self.now_fn()
        factory = self.multi_file_runtime_factory or MultiFilePatchRuntimeCoordinator
        runtime = factory(
            self.config,
            self.journal,
            self.instance_lock,
            fault_hook=self.fault_hook,
        )
        runtime_result = runtime.execute_or_recover(command_id)
        command = self.journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Command disappeared")
        if runtime_result is None:
            return ResultCoordinationOutcome(command_id, command.state, staged=False)
        if command.state is not CommandState.EFFECT_RECORDED:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Multi-file runtime did not record a terminal execution outcome",
            )
        session = self.journal.get_session(command.session_id)
        if session is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Session disappeared")
        staged = build_multi_file_patch_result(
            runtime_result,
            session=session,
            command=command,
        )
        self._fault("AFTER_RESULT_BUILT_BEFORE_STAGE")
        self.journal.stage_result_and_enqueue(
            command_id=command_id,
            result_json=staged.result_json,
            remote_path=staged.remote_path,
            fault_hook=self.fault_hook,
        )
        self._fault("AFTER_STAGE_COMMIT_BEFORE_PUBLISH")
        publication = self.outbox_processor.process_command(command_id)
        updated = self.journal.get_command(command_id)
        assert updated is not None
        return ResultCoordinationOutcome(
            command_id,
            updated.state,
            staged=True,
            publication=publication,
        )

    outbox_common._validate_staged_result = validator
    outbox_staging._validate_staged_result = validator
    result_coordinator_cls.__init__ = patched_init
    result_coordinator_cls.process = patched_process
    setattr(result_coordinator_cls, "_ghb2d_result_installed", True)
