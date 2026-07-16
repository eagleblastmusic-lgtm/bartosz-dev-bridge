from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .execution import ExecutionCoordinator, _diagnostic
from .instance_lock import InstanceLock
from .models import (
    BridgeErrorCode,
    CommandState,
    ExecutionOutcome,
    ProfileRunOutcome,
)
from .multi_file_patch_executor import MultiFilePatchExecutor
from .multi_file_patch_gate import (
    MULTI_FILE_PATCH_OPERATION,
    MULTI_FILE_PATCH_PROFILE,
    validate_multi_file_patch_command,
)
from .multi_file_patch_parser import parse_multi_file_patch
from .multi_file_patch_planner import MultiFilePatchPlanner
from .multi_file_patch_recovery_models import MultiFileCheckpointState
from .multi_file_patch_runtime_models import MultiFilePatchRuntimeResult
from .protocol import BridgeError
from .workspace_manager import WorkspaceManager


FaultHook = Callable[[str], None]
_TERMINAL_WITHOUT_RESULT = frozenset(
    {
        CommandState.MANUAL_RECONCILIATION_REQUIRED,
        CommandState.POLICY_DENIED,
        CommandState.STALE_REVISION,
        CommandState.STATE_MISMATCH,
        CommandState.REJECTED,
        CommandState.EXPIRED,
        CommandState.CANCELLED,
    }
)


class MultiFilePatchRuntimeCoordinator:
    def __init__(
        self,
        config: Any,
        journal: Any,
        instance_lock: InstanceLock,
        *,
        fault_hook: FaultHook | None = None,
        profile_runner: Callable[[WorkspaceManager, str], ProfileRunOutcome] | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.instance_lock = instance_lock
        self.fault_hook = fault_hook
        self.profile_runner = profile_runner or self._default_profile_runner

    def _fault(self, point: str) -> None:
        if self.fault_hook:
            self.fault_hook(point)

    def _default_profile_runner(
        self,
        workspace: WorkspaceManager,
        profile_id: str,
    ) -> ProfileRunOutcome:
        return ExecutionCoordinator(self.config, self.journal)._run_profile(
            workspace,
            profile_id,
        )

    @staticmethod
    def _command_document(command_json: str) -> dict[str, Any]:
        try:
            document = json.loads(command_json)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Persisted multi-file command is invalid JSON",
            ) from exc
        if not isinstance(document, dict):
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Persisted multi-file command must be an object",
            )
        validate_multi_file_patch_command(document)
        return document

    def _workspace(
        self,
        session: Any,
        command_id: str,
    ) -> WorkspaceManager:
        ingestion = self.journal.get_session_ingestion(session.session_id)
        if ingestion is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Session ingestion manifest is missing",
            )
        try:
            manifest = json.loads(ingestion.manifest_json)
            allowed_paths = manifest.get("allowed_paths")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Persisted manifest is invalid JSON",
            ) from exc
        if not isinstance(allowed_paths, list) or not all(
            isinstance(value, str) for value in allowed_paths
        ):
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Persisted manifest allowed_paths is invalid",
            )
        workspace = WorkspaceManager(
            self.config,
            session.session_id,
            session.base_sha,
            allowed_paths,
        )
        try:
            workspace.ensure_workspace(self.journal)
        except BridgeError as exc:
            if exc.code in {
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED.value,
                BridgeErrorCode.DIRTY_SOURCE_CHECKOUT.value,
                BridgeErrorCode.UNKNOWN_BASE_SHA.value,
                BridgeErrorCode.UNSAFE_WORKTREE_PATH.value,
                BridgeErrorCode.GIT_ERROR.value,
            }:
                self._manual(
                    session.session_id,
                    command_id,
                    workspace,
                    reason_code=str(exc.code),
                    summary=f"Workspace attachment failed: {exc}",
                )
            raise
        return workspace

    def _manual(
        self,
        session_id: str,
        command_id: str,
        workspace: WorkspaceManager,
        *,
        reason_code: str,
        summary: str,
    ) -> None:
        checkpoint = self.journal.get_multi_file_patch_checkpoint(command_id)
        if checkpoint is not None and checkpoint.state not in {
            MultiFileCheckpointState.COMMITTED,
            MultiFileCheckpointState.ROLLED_BACK,
            MultiFileCheckpointState.BLOCKED,
        }:
            try:
                self.journal.block_multi_file_patch(command_id, summary)
            except BridgeError:
                pass
        self.journal.mark_workspace_recovery_blocked(
            session_id=session_id,
            command_id=command_id,
            reason_code=reason_code,
            diagnostic=_diagnostic(workspace, reason=summary),
            fault_hook=self.fault_hook,
        )

    def _terminal_claimed(
        self,
        command_id: str,
        state: CommandState,
    ) -> None:
        command = self.journal.get_command(command_id)
        if command is not None and command.state is CommandState.CLAIMED:
            self.journal.transition_command(
                command_id,
                CommandState.CLAIMED,
                state,
            )

    def _ensure_checkpoint(
        self,
        command: Any,
        document: dict[str, Any],
        workspace: WorkspaceManager,
        executor: MultiFilePatchExecutor,
    ) -> None:
        existing = self.journal.get_multi_file_patch_checkpoint(command.command_id)
        if existing is None:
            durable_workspace = self.journal.get_workspace(command.session_id)
            if durable_workspace is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    "Workspace record is missing before multi-file planning",
                )
            workspace.validate_preplan_gate(
                durable_workspace,
                expected_revision=(
                    command.expected_revision
                    if command.expected_revision is not None
                    else -1
                ),
                expected_state_hash=command.expected_state_hash,
            )
            patch = parse_multi_file_patch(document["payload"]["patch"])
            plan = MultiFilePatchPlanner(workspace).plan(patch)
            executor.checkpoint(
                command_id=command.command_id,
                session_id=command.session_id,
                plan=plan,
                fault_hook=self.fault_hook,
            )
            self._fault("AFTER_GHB2D_CHECKPOINT_BEFORE_COMMAND_EXECUTING")
        self.journal.mark_multi_file_patch_command_executing(command.command_id)
        self._fault("AFTER_GHB2D_COMMAND_EXECUTING")

    def _advance_physical_state(
        self,
        command_id: str,
        executor: MultiFilePatchExecutor,
    ) -> None:
        checkpoint = self.journal.get_multi_file_patch_checkpoint(command_id)
        if checkpoint is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CONFLICT,
                "Multi-file checkpoint is missing during execution",
            )
        if checkpoint.state is MultiFileCheckpointState.PLANNED:
            executor.apply(command_id, fault_hook=self.fault_hook)
        elif checkpoint.state is MultiFileCheckpointState.APPLYING:
            executor.recover(command_id)
        elif checkpoint.state in {
            MultiFileCheckpointState.APPLIED,
            MultiFileCheckpointState.COMMITTED,
            MultiFileCheckpointState.ROLLED_BACK,
        }:
            executor.recover(command_id)
        elif checkpoint.state is MultiFileCheckpointState.ROLLING_BACK:
            executor.recover(command_id)
        elif checkpoint.state is MultiFileCheckpointState.BLOCKED:
            raise BridgeError(
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                checkpoint.last_error or "Multi-file checkpoint is blocked",
            )
        else:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"Unsupported checkpoint state {checkpoint.state.value}",
            )

    def _ensure_profile(
        self,
        command_id: str,
        workspace: WorkspaceManager,
    ) -> Any:
        existing = self.journal.get_multi_file_patch_profile_run(command_id)
        if existing is not None:
            return existing
        checkpoint = self.journal.get_multi_file_patch_checkpoint(command_id)
        if checkpoint is None or checkpoint.state is not MultiFileCheckpointState.APPLIED:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                "Profile can run only after a complete batch apply",
            )
        started_at = self.journal._now_fn()
        self._fault("BEFORE_GHB2D_PROFILE")
        outcome = self.profile_runner(workspace, MULTI_FILE_PATCH_PROFILE)
        finished_at = self.journal._now_fn()
        profile = self.journal.record_multi_file_patch_profile_run(
            command_id=command_id,
            profile_id=MULTI_FILE_PATCH_PROFILE,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._fault("AFTER_GHB2D_PROFILE_RECORDED")
        return profile

    def _finish_checkpoint(
        self,
        command_id: str,
        executor: MultiFilePatchExecutor,
        profile: Any,
    ) -> None:
        checkpoint = self.journal.get_multi_file_patch_checkpoint(command_id)
        if checkpoint is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Checkpoint is missing")
        if profile.status == "success":
            if checkpoint.state is MultiFileCheckpointState.APPLIED:
                bundle = self.journal.get_multi_file_patch_bundle(command_id)
                assert bundle is not None
                executor._require_all(bundle, after=True)
                executor._cleanup_all_temps(bundle)
                actual = executor.workspace.compute_state_hash()
                if actual != checkpoint.workspace_state_hash_after:
                    raise BridgeError(
                        BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED,
                        "Workspace hash differs before final batch commit",
                    )
            elif checkpoint.state is not MultiFileCheckpointState.COMMITTED:
                raise BridgeError(
                    BridgeErrorCode.INVALID_STATE_TRANSITION,
                    "Successful profile requires applied checkpoint",
                )
        else:
            if checkpoint.state in {
                MultiFileCheckpointState.APPLIED,
                MultiFileCheckpointState.ROLLING_BACK,
            }:
                if checkpoint.state is MultiFileCheckpointState.APPLIED:
                    executor.rollback(command_id, fault_hook=self.fault_hook)
                else:
                    executor.recover(command_id)
            elif checkpoint.state is not MultiFileCheckpointState.ROLLED_BACK:
                raise BridgeError(
                    BridgeErrorCode.INVALID_STATE_TRANSITION,
                    "Failed profile requires applied/rolling-back checkpoint",
                )
            self._fault("AFTER_GHB2D_ROLLBACK_BEFORE_FINALIZE")
        self.journal.finalize_multi_file_patch_execution(command_id)
        self._fault("AFTER_GHB2D_EXECUTION_RECORDED")

    def _result(
        self,
        command_id: str,
        workspace: WorkspaceManager,
    ) -> MultiFilePatchRuntimeResult:
        bundle = self.journal.get_multi_file_patch_bundle(command_id)
        profile = self.journal.get_multi_file_patch_profile_run(command_id)
        command = self.journal.get_command(command_id)
        if bundle is None or profile is None or command is None:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Final multi-file runtime records are incomplete",
            )
        if command.state is not CommandState.EFFECT_RECORDED:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                "Final multi-file result requires EFFECT_RECORDED command",
            )
        success = profile.status == "success"
        expected_state = (
            MultiFileCheckpointState.COMMITTED
            if success
            else MultiFileCheckpointState.ROLLED_BACK
        )
        if bundle.record.state is not expected_state:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Profile/checkpoint terminal state mismatch",
            )
        attempted = [item.path for item in bundle.paths]
        if success:
            revision_after = bundle.record.workspace_revision_after
            assert revision_after is not None
            state_after = bundle.record.workspace_state_hash_after
            changed_files = attempted
            diff = workspace.git.run(["diff", "--", *attempted]).stdout
            summary = "Multi-file patch committed after successful profile"
        else:
            revision_after = bundle.record.workspace_revision_before
            state_after = bundle.record.workspace_state_hash_before
            changed_files = []
            diff = ""
            summary = "Multi-file patch rolled back after unsuccessful profile"
        outcome = ExecutionOutcome(
            status=profile.status,
            error_code=None if success else profile.status,
            summary=summary,
            workspace_revision_before=bundle.record.workspace_revision_before,
            workspace_revision_after=revision_after,
            workspace_state_hash_before=bundle.record.workspace_state_hash_before,
            workspace_state_hash_after=state_after,
            changed_files=changed_files,
            diff=diff,
            profile_run=profile.to_outcome(),
        )
        return MultiFilePatchRuntimeResult(bundle, profile, outcome)

    def execute_or_recover(self, command_id: str) -> MultiFilePatchRuntimeResult | None:
        command = self.journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Command not found")
        if command.state in _TERMINAL_WITHOUT_RESULT:
            return None
        session = self.journal.get_session(command.session_id)
        if session is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Session not found")
        document = self._command_document(command.command_json)
        workspace = self._workspace(session, command_id)
        executor = MultiFilePatchExecutor(
            workspace,
            self.journal,
            instance_lock=self.instance_lock,
        )
        try:
            if command.state is CommandState.CLAIMED:
                try:
                    self._ensure_checkpoint(command, document, workspace, executor)
                except BridgeError as exc:
                    if exc.code == BridgeErrorCode.STALE_REVISION.value:
                        self._terminal_claimed(command_id, CommandState.STALE_REVISION)
                        return None
                    if exc.code == BridgeErrorCode.STATE_MISMATCH.value:
                        self._terminal_claimed(command_id, CommandState.STATE_MISMATCH)
                        return None
                    if exc.code in {
                        BridgeErrorCode.POLICY_DENIED.value,
                        BridgeErrorCode.SCOPE_VIOLATION.value,
                        BridgeErrorCode.UNSAFE_PATH.value,
                        BridgeErrorCode.INVALID_PAYLOAD.value,
                        BridgeErrorCode.UNSUPPORTED_SCHEMA.value,
                        BridgeErrorCode.UNSUPPORTED_OPERATION.value,
                    }:
                        self._terminal_claimed(command_id, CommandState.POLICY_DENIED)
                        return None
                    raise
                command = self.journal.get_command(command_id)
                assert command is not None
            if command.state is CommandState.EXECUTING:
                self._advance_physical_state(command_id, executor)
                checkpoint = self.journal.get_multi_file_patch_checkpoint(command_id)
                assert checkpoint is not None
                if checkpoint.state in {
                    MultiFileCheckpointState.ROLLED_BACK,
                    MultiFileCheckpointState.COMMITTED,
                }:
                    profile = self.journal.get_multi_file_patch_profile_run(command_id)
                    if profile is None:
                        raise BridgeError(
                            BridgeErrorCode.JOURNAL_CORRUPT,
                            "Terminal checkpoint has no durable profile outcome",
                        )
                else:
                    profile = self._ensure_profile(command_id, workspace)
                self._finish_checkpoint(command_id, executor, profile)
                command = self.journal.get_command(command_id)
                assert command is not None
            if command.state is CommandState.EFFECT_RECORDED:
                return self._result(command_id, workspace)
            if command.state in _TERMINAL_WITHOUT_RESULT:
                return None
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"Unsupported multi-file command state {command.state.value}",
            )
        except BridgeError as exc:
            if exc.code in {
                BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED.value,
                BridgeErrorCode.JOURNAL_CONFLICT.value,
                BridgeErrorCode.JOURNAL_CORRUPT.value,
            }:
                self._manual(
                    command.session_id,
                    command_id,
                    workspace,
                    reason_code=str(exc.code),
                    summary=str(exc),
                )
                return None
            raise
