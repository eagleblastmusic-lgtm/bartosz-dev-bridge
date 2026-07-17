from __future__ import annotations

import json
from typing import Any, Type

from .models import (
    BridgeErrorCode,
    CommandState,
    OutboxState,
    ResultCoordinationOutcome,
    ResultStatus,
    StagedResult,
)
from .protocol import (
    BridgeError,
    SCHEMA_VERSION,
    parse_strict_utc_timestamp,
    result_path_for,
    validate_repo_relative_path,
    validate_strict_utc_timestamp,
)
from .recovery_journal import sha256_bytes
from .serializers import MAX_RESULT_BYTES, finalize_result
from .workspace_manager import WorkspaceManager


OPEN_READ_OPERATION = "open_read"
EXECUTOR_VERSION = "0.7.0-open-read"
_DEFAULT_MAX_LINES = 200
_MAX_LINES = 500
_MAX_CONTENT_BYTES = 8 * 1024


def _document(command_json: str) -> dict[str, Any]:
    try:
        value = json.loads(command_json)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "command_json is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "command_json must be an object")
    return value


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


def _read_payload(command_json: str) -> tuple[str, int, int]:
    document = _document(command_json)
    if document.get("operation") != OPEN_READ_OPERATION:
        raise BridgeError(BridgeErrorCode.UNSUPPORTED_OPERATION, "Command is not open_read")
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "open_read payload must be an object")
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "open_read payload.path must be a non-empty string")
    start_line = payload.get("start_line", 1)
    end_line = payload.get("end_line", start_line + _DEFAULT_MAX_LINES - 1)
    if (
        isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or start_line < 1
        or isinstance(end_line, bool)
        or not isinstance(end_line, int)
        or end_line < start_line
        or end_line - start_line + 1 > _MAX_LINES
    ):
        raise BridgeError(
            BridgeErrorCode.INVALID_RANGE,
            f"open_read requires a 1-based range of at most {_MAX_LINES} lines",
        )
    return validate_repo_relative_path(path), start_line, end_line


def _terminal_state_for(exc: BridgeError) -> CommandState | None:
    code = str(exc.code)
    if code == BridgeErrorCode.STALE_REVISION.value:
        return CommandState.STALE_REVISION
    if code == BridgeErrorCode.STATE_MISMATCH.value:
        return CommandState.STATE_MISMATCH
    if code in {
        BridgeErrorCode.INVALID_PAYLOAD.value,
        BridgeErrorCode.INVALID_RANGE.value,
        BridgeErrorCode.MISSING_FILE.value,
        BridgeErrorCode.POLICY_DENIED.value,
        BridgeErrorCode.SCOPE_VIOLATION.value,
        BridgeErrorCode.UNSAFE_PATH.value,
        BridgeErrorCode.UNSUPPORTED_OPERATION.value,
    }:
        return CommandState.POLICY_DENIED
    return None


def _mark_failure(journal: Any, command: Any, exc: BridgeError, wm: WorkspaceManager | None) -> None:
    terminal = _terminal_state_for(exc)
    if command.state is CommandState.CLAIMED and terminal is not None:
        journal.transition_command(command.command_id, CommandState.CLAIMED, terminal)
        return
    diagnostic: dict[str, object] = {"reason": str(exc)[:200], "error_code": str(exc.code)}
    if wm is not None:
        diagnostic.update(wm.preserve_workspace())
    journal.mark_workspace_recovery_blocked(
        session_id=command.session_id,
        command_id=command.command_id,
        reason_code=str(exc.code),
        diagnostic=diagnostic,
    )


def _execute_open_read(config: Any, journal: Any, command: Any) -> dict[str, Any] | None:
    session = journal.get_session(command.session_id)
    ingestion = journal.get_session_ingestion(command.session_id)
    if session is None or ingestion is None:
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Open-read session or manifest is missing")
    try:
        manifest = json.loads(ingestion.manifest_json)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Persisted manifest is invalid JSON") from exc
    manifest_paths = manifest.get("allowed_paths")
    if not isinstance(manifest_paths, list) or not all(isinstance(item, str) for item in manifest_paths):
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Persisted manifest allowed_paths is invalid")

    wm: WorkspaceManager | None = None
    try:
        path, start_line, requested_end_line = _read_payload(command.command_json)
        wm = WorkspaceManager(config, session.session_id, session.base_sha, manifest_paths)
        workspace = wm.ensure_workspace(journal)
        wm.validate_preplan_gate(
            workspace,
            expected_revision=command.expected_revision if command.expected_revision is not None else -1,
            expected_state_hash=command.expected_state_hash,
        )
        raw = wm.read_exact_bytes(path)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "open_read target must be strict UTF-8") from exc
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        if total_lines == 0:
            if start_line != 1:
                raise BridgeError(BridgeErrorCode.INVALID_RANGE, "open_read start_line is beyond an empty file")
            actual_end_line = 0
            content = ""
        else:
            if start_line > total_lines:
                raise BridgeError(BridgeErrorCode.INVALID_RANGE, "open_read start_line is beyond end of file")
            actual_end_line = min(requested_end_line, total_lines)
            content = "".join(lines[start_line - 1 : actual_end_line])
        encoded = content.encode("utf-8", errors="strict")
        byte_truncated = len(encoded) > _MAX_CONTENT_BYTES
        if byte_truncated:
            content = encoded[:_MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
            encoded = content.encode("utf-8", errors="strict")
        truncated = byte_truncated or (total_lines > 0 and actual_end_line < total_lines)
        if command.state is CommandState.CLAIMED:
            journal.transition_command(command.command_id, CommandState.CLAIMED, CommandState.EXECUTING)
        elif command.state is not CommandState.EXECUTING:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"open_read requires CLAIMED or EXECUTING, got {command.state.value}",
            )
        return {
            "path": path,
            "start_line": start_line,
            "end_line": actual_end_line,
            "total_lines": total_lines,
            "content": content,
            "content_sha256": sha256_bytes(encoded),
            "file_sha256": sha256_bytes(raw),
            "returned_bytes": len(encoded),
            "file_bytes": len(raw),
            "truncated": truncated,
            "workspace_revision": workspace.revision,
            "workspace_state_hash": workspace.state_hash,
        }
    except BridgeError as exc:
        _mark_failure(journal, command, exc, wm)
        return None


def build_open_read_result(
    value: dict[str, Any],
    *,
    session: Any,
    command: Any,
    started_at: str,
    finished_at: str,
) -> StagedResult:
    content = value["content"]
    content_bytes = content.encode("utf-8", errors="strict")
    if sha256_bytes(content_bytes) != value["content_sha256"]:
        raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "open_read content hash mismatch")
    revision = value["workspace_revision"]
    state_hash = value["workspace_state_hash"]
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session.session_id,
        "command_id": command.command_id,
        "sequence": command.sequence,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": _duration_ms(started_at, finished_at),
        "executor_version": EXECUTOR_VERSION,
        "command_commit_sha": command.command_commit_sha,
        "workspace_revision_before": revision,
        "workspace_revision_after": revision,
        "state_hash_before": state_hash,
        "state_hash_after": state_hash,
        "status": ResultStatus.SUCCESS.value,
        "error_code": None,
        "exit_code": 0,
        "summary": f"Read {value['path']} lines {value['start_line']}-{value['end_line']}",
        "stdout_tail": "",
        "stderr_tail": "",
        "stdout_sha256": sha256_bytes(b""),
        "stderr_sha256": sha256_bytes(b""),
        "changed_files": [],
        "diff": "",
        "diff_sha256": sha256_bytes(b""),
        "artifacts": [],
        "data": {
            "operation": OPEN_READ_OPERATION,
            "path": value["path"],
            "start_line": value["start_line"],
            "end_line": value["end_line"],
            "total_lines": value["total_lines"],
            "content": content,
            "content_sha256": value["content_sha256"],
            "file_sha256": value["file_sha256"],
            "returned_bytes": value["returned_bytes"],
            "file_bytes": value["file_bytes"],
        },
        "truncated": bool(value["truncated"]),
    }
    result_json = finalize_result(result)
    result_bytes = result_json.encode("utf-8", errors="strict")
    if len(result_bytes) > MAX_RESULT_BYTES:
        raise BridgeError(BridgeErrorCode.RESULT_TOO_LARGE, "Final open_read result exceeds the result limit")
    return StagedResult(
        command_id=command.command_id,
        result_json=result_json,
        result_bytes=result_bytes,
        result_sha256=sha256_bytes(result_bytes),
        remote_path=result_path_for(session.session_id, command.sequence),
    )


def _validate_open_read_result(journal: Any, command_id: str, result_json: str, remote_path: str):
    from . import outbox_common as common

    try:
        result_bytes = result_json.encode("utf-8", errors="strict")
        parsed = json.loads(result_json)
    except (UnicodeEncodeError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid strict UTF-8 open_read result") from exc
    if len(result_bytes) > MAX_RESULT_BYTES or not isinstance(parsed, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid bounded open_read result")
    command = journal.get_command(command_id)
    workspace = journal.get_workspace(command.session_id) if command is not None else None
    if command is None or workspace is None:
        raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Open-read command or workspace is missing")
    if command.state not in {CommandState.EXECUTING, CommandState.RESULT_STAGED, CommandState.RESULT_PUBLISHED}:
        raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Open-read result has invalid command state")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "session_id": command.session_id,
        "command_id": command.command_id,
        "sequence": command.sequence,
        "command_commit_sha": command.command_commit_sha,
        "workspace_revision_before": workspace.revision,
        "workspace_revision_after": workspace.revision,
        "state_hash_before": workspace.state_hash,
        "state_hash_after": workspace.state_hash,
        "status": ResultStatus.SUCCESS.value,
        "error_code": None,
        "exit_code": 0,
        "changed_files": [],
        "diff": "",
        "artifacts": [],
    }
    if any(parsed.get(field) != expected_value for field, expected_value in expected.items()):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Open-read result differs from durable state")
    for field in ("started_at", "finished_at"):
        validate_strict_utc_timestamp(parsed.get(field), field=f"result.{field}")
    if parsed.get("duration_ms") != _duration_ms(parsed["started_at"], parsed["finished_at"]):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Open-read duration mismatch")
    data = parsed.get("data")
    payload = _document(command.command_json).get("payload")
    if not isinstance(data, dict) or not isinstance(payload, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Open-read result data is missing")
    if data.get("operation") != OPEN_READ_OPERATION or data.get("path") != payload.get("path"):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Open-read result identity mismatch")
    content = data.get("content")
    if not isinstance(content, str) or data.get("content_sha256") != sha256_bytes(content.encode("utf-8")):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Open-read content hash mismatch")
    common._require_hash(data.get("file_sha256"), "result.data.file_sha256")
    if data.get("returned_bytes") != len(content.encode("utf-8")):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Open-read returned byte count mismatch")
    for field in ("start_line", "end_line", "total_lines", "file_bytes"):
        if type(data.get(field)) is not int or data[field] < 0:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"Open-read {field} must be non-negative")
    for text_field, hash_field in (
        ("stdout_tail", "stdout_sha256"),
        ("stderr_tail", "stderr_sha256"),
        ("diff", "diff_sha256"),
    ):
        text = parsed.get(text_field)
        if not isinstance(text, str) or parsed.get(hash_field) != sha256_bytes(text.encode("utf-8")):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"Open-read {text_field} hash mismatch")
    if type(parsed.get("truncated")) is not bool:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Open-read truncated must be boolean")
    common._validate_end_marker(parsed)
    expected_path = result_path_for(command.session_id, command.sequence)
    validate_repo_relative_path(remote_path)
    if remote_path != expected_path:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Invalid open-read result path")
    return parsed, result_bytes, parsed["status"], parsed.get("error_code"), command


def _stage_open_read_result(
    journal: Any,
    *,
    command_id: str,
    result_json: str,
    remote_path: str,
    fault_hook: Any = None,
):
    from . import outbox_common as common

    _parsed, result_bytes, status, error_code, _command = _validate_open_read_result(
        journal, command_id, result_json, remote_path
    )
    result_sha256 = sha256_bytes(result_bytes)
    now = journal._now_fn()
    validate_strict_utc_timestamp(now, field="now")
    with journal._transaction():
        existing_result = journal.get_result(command_id)
        existing_outbox = common.get_outbox(journal, command_id)
        current = journal.get_command(command_id)
        if current is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command not found: {command_id}")
        if existing_result is not None or existing_outbox is not None:
            if existing_result is None or existing_outbox is None:
                raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Result/outbox atomicity invariant is broken")
            if not common._result_matches(
                existing_result,
                result_json=result_json,
                result_sha256=result_sha256,
                remote_path=remote_path,
                status=status,
                error_code=error_code,
            ) or not common._outbox_matches(existing_outbox, existing_result):
                raise BridgeError(BridgeErrorCode.RESULT_COLLISION, f"Result collision for command {command_id}")
            return existing_result, existing_outbox
        if current.state is not CommandState.EXECUTING:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "New open-read result requires EXECUTING")
        occupied = journal._connection.execute(
            "SELECT command_id FROM results WHERE session_id = ? AND sequence = ?",
            (current.session_id, current.sequence),
        ).fetchone()
        path_occupied = journal._connection.execute(
            "SELECT command_id FROM outbox WHERE remote_path = ?", (remote_path,)
        ).fetchone()
        if occupied is not None or path_occupied is not None:
            raise BridgeError(BridgeErrorCode.RESULT_COLLISION, "Result sequence or path is occupied")
        journal._connection.execute(
            """
            INSERT INTO results (
                command_id, session_id, sequence, status, error_code,
                result_sha256, result_json, remote_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                current.session_id,
                current.sequence,
                status,
                error_code,
                result_sha256,
                result_json,
                remote_path,
                now,
            ),
        )
        if fault_hook:
            fault_hook("AFTER_RESULT_INSERT")
        journal._connection.execute(
            """
            INSERT INTO outbox (
                command_id, session_id, sequence, result_sha256, remote_path,
                state, attempt_count, next_attempt_at, last_error,
                published_commit_sha, published_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (
                command_id,
                current.session_id,
                current.sequence,
                result_sha256,
                remote_path,
                OutboxState.PENDING.value,
                now,
                now,
            ),
        )
        if fault_hook:
            fault_hook("AFTER_OUTBOX_INSERT")
            fault_hook("BEFORE_RESULT_STAGED_TRANSITION")
        journal._transition_command_in_transaction(
            command_id=command_id,
            expected_state=CommandState.EXECUTING,
            new_state=CommandState.RESULT_STAGED,
            now=now,
        )
        journal._append_event_in_transaction(
            session_id=current.session_id,
            command_id=command_id,
            event_type="result.staged",
            payload={"sequence": current.sequence, "result_sha256": result_sha256},
            created_at=now,
        )
        journal._append_event_in_transaction(
            session_id=current.session_id,
            command_id=command_id,
            event_type="outbox.enqueued",
            payload={"remote_path": remote_path},
            created_at=now,
        )
    result = journal.get_result(command_id)
    outbox = common.get_outbox(journal, command_id)
    assert result is not None and outbox is not None
    return result, outbox


def install_open_read_result_support(result_coordinator_cls: Type[object], journal_cls: Type[object]) -> None:
    if getattr(result_coordinator_cls, "_open_read_result_installed", False):
        return
    original_process = result_coordinator_cls.process
    original_stage = journal_cls.stage_result_and_enqueue

    def patched_stage(
        self: Any,
        *,
        command_id: str,
        result_json: str,
        remote_path: str,
        fault_hook: Any = None,
    ):
        command = self.get_command(command_id)
        if command is not None and _operation(command.command_json) == OPEN_READ_OPERATION:
            return _stage_open_read_result(
                self,
                command_id=command_id,
                result_json=result_json,
                remote_path=remote_path,
                fault_hook=fault_hook,
            )
        return original_stage(
            self,
            command_id=command_id,
            result_json=result_json,
            remote_path=remote_path,
            fault_hook=fault_hook,
        )

    def patched_process(self: Any, command_id: str) -> ResultCoordinationOutcome:
        command = self.journal.get_command(command_id)
        if command is None or _operation(command.command_json) != OPEN_READ_OPERATION:
            return original_process(self, command_id)
        if command.state in {CommandState.RESULT_STAGED, CommandState.RESULT_PUBLISHED}:
            return original_process(self, command_id)
        if command.state not in {CommandState.CLAIMED, CommandState.EXECUTING}:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"Open-read coordination cannot use {command.state.value}",
            )
        started_at = self.now_fn()
        read_value = _execute_open_read(self.config, self.journal, command)
        command = self.journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Open-read command disappeared")
        if read_value is None:
            return ResultCoordinationOutcome(command_id, command.state, staged=False)
        session = self.journal.get_session(command.session_id)
        if session is None or command.state is not CommandState.EXECUTING:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Open-read durable state is incomplete")
        staged = build_open_read_result(
            read_value,
            session=session,
            command=command,
            started_at=started_at,
            finished_at=self.now_fn(),
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
        return ResultCoordinationOutcome(command_id, updated.state, staged=True, publication=publication)

    journal_cls.stage_result_and_enqueue = patched_stage
    result_coordinator_cls.process = patched_process
    setattr(result_coordinator_cls, "_open_read_result_installed", True)
