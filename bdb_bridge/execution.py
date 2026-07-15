from __future__ import annotations

import os
import time
import subprocess
import json
from typing import Any, Callable

from .models import (
    BridgeErrorCode,
    CommandState,
    SessionState,
    OperationPlanRecord,
    OperationEffectRecord,
    RecoveryDecision,
    ProfileRunOutcome,
    ExecutionOutcome,
    TERMINAL_COMMAND_STATES,
    TERMINAL_SESSION_STATES,
)
from .protocol import BridgeError, parse_manifest_path
from .serializers import sha256_text
from .workspace_manager import WorkspaceManager
from .journal import Journal

class SystemCrash(BaseException):
    pass

def sanitized_test_environment() -> dict[str, str]:
    allowed = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
    return env

def make_recovery_decision(
    command_id: str,
    cmd_state: CommandState,
    journal: Journal,
    wm: WorkspaceManager,
) -> tuple[RecoveryDecision, OperationPlanRecord | None, OperationEffectRecord | None]:
    if cmd_state == CommandState.CLAIMED:
        return RecoveryDecision.EXECUTE, None, None

    plan = journal.get_operation_plan(command_id)
    if plan is None:
        return RecoveryDecision.DIVERGED, None, None

    jw = journal.get_workspace(plan.session_id)
    if jw is None:
        return RecoveryDecision.DIVERGED, plan, None

    try:
        target_bytes = wm.read_exact_bytes(plan.target_path)
    except Exception:
        return RecoveryDecision.DIVERGED, plan, None

    actual_state_hash = wm.compute_state_hash()

    # Check if there are any unauthorized untracked/modified paths
    paths = wm.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
    for p in paths:
        if not wm.is_allowed_path(p):
            return RecoveryDecision.DIVERGED, plan, None

    if cmd_state == CommandState.EXECUTING:
        # Check BEFORE condition
        if (
            target_bytes == plan.before_content
            and actual_state_hash == plan.workspace_state_hash_before
            and jw.revision == plan.workspace_revision_before
            and jw.state_hash == plan.workspace_state_hash_before
        ):
            return RecoveryDecision.EXECUTE, plan, None

        # Check PLANNED-AFTER condition
        if (
            target_bytes == plan.planned_after_content
            and actual_state_hash == plan.planned_after_state_hash
            and jw.revision == plan.workspace_revision_before
            and jw.state_hash == plan.workspace_state_hash_before
        ):
            return RecoveryDecision.RECOVER_PLANNED_AFTER, plan, None

        return RecoveryDecision.DIVERGED, plan, None

    elif cmd_state == CommandState.EFFECT_RECORDED:
        effect = journal.get_operation_effect(command_id)
        if effect is None:
            return RecoveryDecision.DIVERGED, plan, None

        # Check IDEMPOTENT_REPLAY condition
        if (
            target_bytes == plan.planned_after_content
            and actual_state_hash == effect.workspace_state_hash_after
            and jw.revision == effect.workspace_revision_after
            and jw.state_hash == effect.workspace_state_hash_after
        ):
            return RecoveryDecision.IDEMPOTENT_REPLAY, plan, effect

        return RecoveryDecision.DIVERGED, plan, effect

    return RecoveryDecision.DIVERGED, None, None


class ExecutionCoordinator:
    def __init__(
        self,
        config: Any,
        journal: Journal,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.fault_hook = fault_hook

    def execute_or_recover(self, command_id: str) -> ExecutionOutcome:
        self.journal._ensure_open()

        cmd = self.journal.get_command(command_id)
        if cmd is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Command {command_id} not found")

        session_id = cmd.session_id
        session = self.journal.get_session(session_id)
        if session is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Session {session_id} not found")

        session_ing = self.journal.get_session_ingestion(session_id)
        if session_ing is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, f"Session ingestion manifest not found for {session_id}")

        try:
            manifest_data = json.loads(session_ing.manifest_json)
            manifest_paths = list(manifest_data.get("allowed_paths", []))
        except Exception as exc:
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, f"Failed to parse manifest JSON: {exc}") from exc

        wm = WorkspaceManager(self.config, session_id, session.base_sha, manifest_paths)

        try:
            wm.ensure_workspace(self.journal)
        except BridgeError as exc:
            if exc.code in (BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, BridgeErrorCode.DIRTY_SOURCE_CHECKOUT, BridgeErrorCode.UNKNOWN_BASE_SHA):
                # Transition session and command to manual reconciliation
                with self.journal._transaction():
                    self.journal.transition_command(command_id, cmd.state, CommandState.MANUAL_RECONCILIATION_REQUIRED)
                    self.journal.transition_session(session_id, session.state, SessionState.MANUAL_RECONCILIATION_REQUIRED)
                    self.journal._append_event_in_transaction(
                        session_id=session_id,
                        command_id=command_id,
                        event_type="workspace.recovery_blocked",
                        payload={"reason": str(exc)},
                        created_at=self.journal._now_fn(),
                    )
                return ExecutionOutcome(
                    status="manual_reconciliation_required",
                    error_code=BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                    summary=f"Workspace attachment failed: {exc}",
                    workspace_revision_before=0,
                    workspace_revision_after=0,
                    workspace_state_hash_before="",
                    workspace_state_hash_after="",
                    changed_files=[],
                    diff="",
                    manual_reconciliation_details=wm.preserve_workspace(),
                )
            raise

        try:
            decision, plan, effect = make_recovery_decision(command_id, cmd.state, self.journal, wm)
        except Exception as exc:
            decision = RecoveryDecision.DIVERGED
            plan = None
            effect = None

        if decision == RecoveryDecision.DIVERGED:
            with self.journal._transaction():
                curr_cmd = self.journal.get_command(command_id)
                curr_sess = self.journal.get_session(session_id)
                if curr_cmd and curr_cmd.state not in TERMINAL_COMMAND_STATES:
                    self.journal.transition_command(command_id, curr_cmd.state, CommandState.MANUAL_RECONCILIATION_REQUIRED)
                if curr_sess and curr_sess.state not in TERMINAL_SESSION_STATES:
                    self.journal.transition_session(session_id, curr_sess.state, SessionState.MANUAL_RECONCILIATION_REQUIRED)

                existing_event = self.journal._connection.execute(
                    "SELECT 1 FROM events WHERE session_id = ? AND command_id = ? AND event_type = 'workspace.recovery_blocked'",
                    (session_id, command_id)
                ).fetchone()
                if existing_event is None:
                    self.journal._append_event_in_transaction(
                        session_id=session_id,
                        command_id=command_id,
                        event_type="workspace.recovery_blocked",
                        payload={"reason": "Workspace state divergence detected"},
                        created_at=self.journal._now_fn(),
                    )
            return ExecutionOutcome(
                status="manual_reconciliation_required",
                error_code=BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                summary="Workspace state diverged from expected values",
                workspace_revision_before=0,
                workspace_revision_after=0,
                workspace_state_hash_before="",
                workspace_state_hash_after="",
                changed_files=[],
                diff="",
                manual_reconciliation_details=wm.preserve_workspace(),
            )

        elif decision == RecoveryDecision.IDEMPOTENT_REPLAY:
            assert plan is not None
            assert effect is not None
            if self.fault_hook:
                self.fault_hook("BEFORE_PROFILE")
            profile_run = self._run_profile(wm)
            return ExecutionOutcome(
                status=profile_run.status,
                error_code=None if profile_run.status == "success" else profile_run.status,
                summary="Idempotent replay outcome",
                workspace_revision_before=effect.workspace_revision_before,
                workspace_revision_after=effect.workspace_revision_after,
                workspace_state_hash_before=effect.workspace_state_hash_before,
                workspace_state_hash_after=effect.workspace_state_hash_after,
                changed_files=self._get_changed_files(wm),
                diff=wm.git.run(["diff", "--", plan.target_path]).stdout,
                profile_run=profile_run,
            )

        elif decision == RecoveryDecision.RECOVER_PLANNED_AFTER:
            assert plan is not None
            effect_rec = OperationEffectRecord(
                command_id=command_id,
                session_id=session_id,
                plan_sha256=plan.plan_sha256,
                target_path=plan.target_path,
                workspace_revision_before=plan.workspace_revision_before,
                workspace_revision_after=plan.workspace_revision_before + 1,
                workspace_state_hash_before=plan.workspace_state_hash_before,
                workspace_state_hash_after=plan.planned_after_state_hash,
                before_content_sha256=plan.before_content_sha256,
                after_content_sha256=plan.planned_after_content_sha256,
                effect_sha256=sha256_text(plan.plan_sha256 + plan.planned_after_state_hash),
                recorded_at=self.journal._now_fn(),
            )
            self.journal.record_operation_effect(effect_rec)

            if self.fault_hook:
                self.fault_hook("AFTER_EFFECT_COMMIT_BEFORE_PROFILE")
            if self.fault_hook:
                self.fault_hook("BEFORE_PROFILE")

            profile_run = self._run_profile(wm)
            return ExecutionOutcome(
                status=profile_run.status,
                error_code=None if profile_run.status == "success" else profile_run.status,
                summary="Recover planned after outcome",
                workspace_revision_before=effect_rec.workspace_revision_before,
                workspace_revision_after=effect_rec.workspace_revision_after,
                workspace_state_hash_before=effect_rec.workspace_state_hash_before,
                workspace_state_hash_after=effect_rec.workspace_state_hash_after,
                changed_files=self._get_changed_files(wm),
                diff=wm.git.run(["diff", "--", plan.target_path]).stdout,
                profile_run=profile_run,
            )

        elif decision == RecoveryDecision.EXECUTE:
            if plan is None:
                try:
                    payload = json.loads(cmd.command_json).get("payload", {})
                    operation = json.loads(cmd.command_json).get("operation")
                    profile_id = json.loads(cmd.command_json).get("profile_id", "poc_pytest")
                except Exception as exc:
                    raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"Failed to parse command JSON: {exc}") from exc

                if operation != "replace_exact_and_test":
                    raise BridgeError(BridgeErrorCode.UNSUPPORTED_OPERATION, f"Operation {operation} is not supported")
                if profile_id != "poc_pytest":
                    raise BridgeError(BridgeErrorCode.POLICY_DENIED, "Only profile_id=poc_pytest is allowed")

                relative_path = payload.get("path")
                old_text = payload.get("old")
                new_text = payload.get("new")
                if not relative_path or old_text is None or new_text is None:
                    raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Missing path, old, or new keys in payload")

                jw = self.journal.get_workspace(session_id)
                assert jw is not None
                if cmd.expected_revision != jw.revision:
                    self.journal.transition_command(command_id, cmd.state, CommandState.STALE_REVISION)
                    return ExecutionOutcome(
                        status="stale_revision",
                        error_code=BridgeErrorCode.STALE_REVISION,
                        summary=f"Expected revision {cmd.expected_revision}, current revision is {jw.revision}",
                        workspace_revision_before=jw.revision,
                        workspace_revision_after=jw.revision,
                        workspace_state_hash_before=jw.state_hash,
                        workspace_state_hash_after=jw.state_hash,
                        changed_files=[],
                        diff="",
                    )
                if cmd.expected_state_hash is not None and cmd.expected_state_hash != jw.state_hash:
                    self.journal.transition_command(command_id, cmd.state, CommandState.STATE_MISMATCH)
                    return ExecutionOutcome(
                        status="state_mismatch",
                        error_code=BridgeErrorCode.STATE_MISMATCH,
                        summary="expected_state_hash does not match workspace",
                        workspace_revision_before=jw.revision,
                        workspace_revision_after=jw.revision,
                        workspace_state_hash_before=jw.state_hash,
                        workspace_state_hash_after=jw.state_hash,
                        changed_files=[],
                        diff="",
                    )

                wm.resolve_allowed_path(relative_path)
                before_bytes = wm.read_exact_bytes(relative_path)
                try:
                    before_text = before_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"Strict UTF-8 decode failed for target file: {exc}") from exc

                match_count = before_text.count(old_text)
                if match_count != 1:
                    raise BridgeError(BridgeErrorCode.REPLACE_MISMATCH, f"Expected exactly one match for replace, found {match_count}")

                after_text = before_text.replace(old_text, new_text, 1)
                planned_after_bytes = after_text.encode("utf-8")

                before_hash = sha256_text(before_text)
                planned_after_hash = sha256_text(after_text)

                workspace_state_hash_before = wm.compute_state_hash()
                planned_after_state_hash = wm.compute_state_hash_with_override(relative_path, planned_after_bytes)

                plan_sha256 = sha256_text(f"v1:{command_id}:{planned_after_state_hash}")

                plan = OperationPlanRecord(
                    command_id=command_id,
                    session_id=session_id,
                    operation=operation,
                    target_path=relative_path,
                    profile_id=profile_id,
                    expected_revision=cmd.expected_revision,
                    expected_state_hash=cmd.expected_state_hash,
                    workspace_revision_before=jw.revision,
                    workspace_state_hash_before=workspace_state_hash_before,
                    before_content=before_bytes,
                    before_content_sha256=before_hash,
                    planned_after_content=planned_after_bytes,
                    planned_after_content_sha256=planned_after_hash,
                    planned_after_state_hash=planned_after_state_hash,
                    plan_sha256=plan_sha256,
                    created_at=self.journal._now_fn(),
                )
                self.journal.record_operation_plan(plan)

            if self.fault_hook:
                self.fault_hook("AFTER_PLAN_COMMIT_BEFORE_WRITE")

            def on_temp_written():
                if self.fault_hook:
                    self.fault_hook("AFTER_TEMP_WRITE_BEFORE_REPLACE")

            self._write_file_with_temp(wm, plan.target_path, plan.planned_after_content, on_temp_written)

            written_bytes = wm.read_exact_bytes(plan.target_path)
            if written_bytes != plan.planned_after_content:
                raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Written bytes mismatch from planned content")

            actual_state_hash = wm.compute_state_hash()
            if actual_state_hash != plan.planned_after_state_hash:
                raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, "Post-write state hash mismatch from planned state hash")

            if self.fault_hook:
                self.fault_hook("AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT")

            effect_rec = OperationEffectRecord(
                command_id=command_id,
                session_id=session_id,
                plan_sha256=plan.plan_sha256,
                target_path=plan.target_path,
                workspace_revision_before=plan.workspace_revision_before,
                workspace_revision_after=plan.workspace_revision_before + 1,
                workspace_state_hash_before=plan.workspace_state_hash_before,
                workspace_state_hash_after=plan.planned_after_state_hash,
                before_content_sha256=plan.before_content_sha256,
                after_content_sha256=plan.planned_after_content_sha256,
                effect_sha256=sha256_text(plan.plan_sha256 + plan.planned_after_state_hash),
                recorded_at=self.journal._now_fn(),
            )
            self.journal.record_operation_effect(effect_rec)

            if self.fault_hook:
                self.fault_hook("AFTER_EFFECT_COMMIT_BEFORE_PROFILE")
            if self.fault_hook:
                self.fault_hook("BEFORE_PROFILE")

            profile_run = self._run_profile(wm)
            return ExecutionOutcome(
                status=profile_run.status,
                error_code=None if profile_run.status == "success" else profile_run.status,
                summary="Command executed successfully",
                workspace_revision_before=effect_rec.workspace_revision_before,
                workspace_revision_after=effect_rec.workspace_revision_after,
                workspace_state_hash_before=effect_rec.workspace_state_hash_before,
                workspace_state_hash_after=effect_rec.workspace_state_hash_after,
                changed_files=self._get_changed_files(wm),
                diff=wm.git.run(["diff", "--", plan.target_path]).stdout,
                profile_run=profile_run,
            )

        raise BridgeError(BridgeErrorCode.UNSUPPORTED_OPERATION, f"Invalid recovery decision: {decision.value}")

    def _write_file_with_temp(
        self,
        wm: WorkspaceManager,
        relative_path: str,
        content: bytes,
        on_temp_written: Callable[[], None] | None = None,
    ) -> None:
        path = wm.resolve_allowed_path(relative_path)
        dir_path = path.parent
        temp_name = f".bdb_temp_{path.name}"
        temp_path = dir_path / temp_name
        try:
            temp_path.write_bytes(content)
            fd = os.open(temp_path, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if on_temp_written:
                on_temp_written()
            os.replace(temp_path, path)
        except Exception as exc:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise BridgeError(BridgeErrorCode.JOURNAL_CORRUPT, f"Failed to write file atomically: {exc}") from exc

    def _run_profile(self, wm: WorkspaceManager) -> ProfileRunOutcome:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [self.config.python_executable, "-m", "pytest", "-q"],
                cwd=wm.path,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.config.test_timeout_seconds,
                env=sanitized_test_environment(),
            )
            status = "success" if completed.returncode == 0 else "failed"
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            exit_code = None
            stdout_bytes = exc.stdout or b""
            stderr_bytes = exc.stderr or b""
            stdout = stdout_bytes.decode("utf-8", errors="replace") if isinstance(stdout_bytes, bytes) else stdout_bytes
            stderr = stderr_bytes.decode("utf-8", errors="replace") if isinstance(stderr_bytes, bytes) else stderr_bytes
        except Exception as exc:
            status = "internal_error"
            exit_code = None
            stdout = ""
            stderr = str(exc)

        duration_ms = int((time.monotonic() - started) * 1000)
        return ProfileRunOutcome(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        )

    def _get_changed_files(self, wm: WorkspaceManager) -> list[str]:
        res = wm.git.run(["status", "--porcelain=v1"])
        from bdb_poc.common import changed_paths
        return changed_paths(res.stdout)
