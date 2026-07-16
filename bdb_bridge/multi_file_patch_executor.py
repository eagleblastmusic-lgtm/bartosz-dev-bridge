from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable

from .models import BridgeErrorCode
from .multi_file_patch_journal import compute_multi_file_checkpoint_sha256
from .multi_file_patch_models import MultiFilePatchPlan
from .multi_file_patch_planner import MultiFilePatchPlanner
from .multi_file_patch_recovery_models import (
    MultiFileCheckpointBundle,
    MultiFileCheckpointPath,
    MultiFileCheckpointState,
    MultiFileRecoveryOutcome,
)
from .protocol import BridgeError, validate_repo_relative_path
from .workspace_manager import WorkspaceManager


FaultHook = Callable[[str], None]


class MultiFilePatchExecutor:
    """Durable apply/rollback/commit coordinator for one planned multi-file patch."""

    def __init__(self, workspace: WorkspaceManager, journal: Any) -> None:
        self.workspace = workspace
        self.journal = journal
        self.planner = MultiFilePatchPlanner(workspace)

    def checkpoint(
        self,
        *,
        command_id: str,
        session_id: str,
        plan: MultiFilePatchPlan,
        fault_hook: FaultHook | None = None,
    ) -> MultiFileCheckpointBundle:
        self.planner.revalidate(plan)
        workspace_record = self.journal.get_workspace(session_id)
        if workspace_record is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Workspace is not registered")
        if self.workspace.session_id != session_id:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Executor workspace/session mismatch")
        actual_before = self.workspace.compute_state_hash()
        if actual_before != workspace_record.state_hash:
            raise BridgeError(
                BridgeErrorCode.STATE_MISMATCH,
                "Physical workspace state differs from Journal before checkpoint",
            )
        path_by_name = {item.path: item for item in plan.paths}
        changed = [path_by_name[path] for path in plan.changed_paths]
        paths = tuple(
            MultiFileCheckpointPath(
                command_id=command_id,
                ordinal=ordinal,
                path=item.path,
                before_exists=item.before_exists,
                before=item.before,
                before_sha256=item.before_sha256,
                after_exists=item.after_exists,
                after=item.after,
                after_sha256=item.after_sha256,
                roles=item.roles,
                operation_indices=item.operation_indices,
            )
            for ordinal, item in enumerate(changed)
        )
        after_hash = self._predicted_state_hash(paths, after=True)
        checkpoint_sha256 = compute_multi_file_checkpoint_sha256(
            command_id=command_id,
            session_id=session_id,
            patch_sha256=plan.patch.patch_sha256,
            plan_sha256=plan.plan_sha256,
            workspace_revision_before=workspace_record.revision,
            workspace_state_hash_before=workspace_record.state_hash,
            workspace_state_hash_after=after_hash,
            paths=paths,
        )
        self.journal.record_multi_file_patch_checkpoint(
            command_id=command_id,
            session_id=session_id,
            patch_sha256=plan.patch.patch_sha256,
            plan_sha256=plan.plan_sha256,
            checkpoint_sha256=checkpoint_sha256,
            workspace_revision_before=workspace_record.revision,
            workspace_state_hash_before=workspace_record.state_hash,
            workspace_state_hash_after=after_hash,
            paths=paths,
        )
        if fault_hook:
            fault_hook("AFTER_BATCH_CHECKPOINT")
        bundle = self.journal.get_multi_file_patch_bundle(command_id)
        assert bundle is not None
        return bundle

    def apply(
        self,
        command_id: str,
        *,
        fault_hook: FaultHook | None = None,
    ) -> MultiFileRecoveryOutcome:
        bundle = self._require_bundle(command_id)
        if bundle.record.state is MultiFileCheckpointState.COMMITTED:
            return self._outcome(bundle, "already_committed")
        if bundle.record.state is MultiFileCheckpointState.APPLIED:
            self._require_all(bundle, after=True)
            return self._outcome(bundle, "already_applied")
        if bundle.record.state is MultiFileCheckpointState.PLANNED:
            self.journal.mark_multi_file_patch_applying(command_id)
            if fault_hook:
                fault_hook("AFTER_BATCH_APPLYING")
            bundle = self._require_bundle(command_id)
        if bundle.record.state is not MultiFileCheckpointState.APPLYING:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                f"Cannot apply checkpoint in {bundle.record.state.value}",
            )

        try:
            for item in bundle.paths:
                if not item.after_exists:
                    continue
                status = self._classify(item)
                if status == "after":
                    self._cleanup_temps(bundle, item)
                    continue
                if status != "before":
                    self._block(bundle, f"Unexpected bytes before applying {item.path}")
                assert item.after is not None
                self._write_exact(
                    bundle,
                    item,
                    expected=item.before,
                    target=item.after,
                    mode="apply",
                    fault_hook=fault_hook,
                )
                if fault_hook:
                    fault_hook(f"AFTER_BATCH_PATH_APPLIED:{item.ordinal}")

            for item in bundle.paths:
                if item.after_exists:
                    continue
                status = self._classify(item)
                if status == "after":
                    self._cleanup_temps(bundle, item)
                    continue
                if status != "before":
                    self._block(bundle, f"Unexpected bytes before deleting {item.path}")
                self._delete_exact(item, expected=item.before)
                self._cleanup_temps(bundle, item)
                if fault_hook:
                    fault_hook(f"AFTER_BATCH_PATH_APPLIED:{item.ordinal}")

            self._require_all(bundle, after=True)
            self._cleanup_all_temps(bundle)
            if fault_hook:
                fault_hook("BEFORE_BATCH_APPLIED_RECORD")
            self.journal.mark_multi_file_patch_applied(command_id)
        except BridgeError:
            raise
        except OSError as exc:
            self._block(bundle, f"Controlled batch apply failure: {type(exc).__name__}")
        return self._outcome(self._require_bundle(command_id), "applied")

    def rollback(
        self,
        command_id: str,
        *,
        fault_hook: FaultHook | None = None,
    ) -> MultiFileRecoveryOutcome:
        bundle = self._require_bundle(command_id)
        if bundle.record.state is MultiFileCheckpointState.ROLLED_BACK:
            self._require_all(bundle, after=False)
            return self._outcome(bundle, "already_rolled_back")
        if bundle.record.state is MultiFileCheckpointState.COMMITTED:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                "Committed batch cannot be rolled back by the same checkpoint",
            )
        if bundle.record.state is not MultiFileCheckpointState.ROLLING_BACK:
            self.journal.mark_multi_file_patch_rolling_back(command_id)
            if fault_hook:
                fault_hook("AFTER_BATCH_ROLLING_BACK")
            bundle = self._require_bundle(command_id)

        try:
            for item in bundle.paths:
                if not item.before_exists:
                    continue
                status = self._classify(item)
                if status == "before":
                    self._cleanup_temps(bundle, item)
                    continue
                if status != "after":
                    self._block(bundle, f"Unexpected bytes before restoring {item.path}")
                assert item.before is not None
                self._write_exact(
                    bundle,
                    item,
                    expected=item.after,
                    target=item.before,
                    mode="rollback",
                    fault_hook=fault_hook,
                )
                if fault_hook:
                    fault_hook(f"AFTER_BATCH_PATH_ROLLED_BACK:{item.ordinal}")

            for item in bundle.paths:
                if item.before_exists:
                    continue
                status = self._classify(item)
                if status == "before":
                    self._cleanup_temps(bundle, item)
                    continue
                if status != "after":
                    self._block(bundle, f"Unexpected bytes before removing created path {item.path}")
                self._delete_exact(item, expected=item.after)
                self._cleanup_temps(bundle, item)
                if fault_hook:
                    fault_hook(f"AFTER_BATCH_PATH_ROLLED_BACK:{item.ordinal}")

            self._require_all(bundle, after=False)
            self._cleanup_all_temps(bundle)
            actual = self.workspace.compute_state_hash()
            if actual != bundle.record.workspace_state_hash_before:
                self._block(bundle, "Workspace hash differs after rollback")
            if fault_hook:
                fault_hook("BEFORE_BATCH_ROLLED_BACK_RECORD")
            self.journal.mark_multi_file_patch_rolled_back(command_id)
        except BridgeError:
            raise
        except OSError as exc:
            self._block(bundle, f"Controlled batch rollback failure: {type(exc).__name__}")
        return self._outcome(self._require_bundle(command_id), "rolled_back")

    def commit(self, command_id: str) -> MultiFileRecoveryOutcome:
        bundle = self._require_bundle(command_id)
        if bundle.record.state is MultiFileCheckpointState.COMMITTED:
            self._require_all(bundle, after=True)
            return self._outcome(bundle, "already_committed")
        if bundle.record.state is not MultiFileCheckpointState.APPLIED:
            raise BridgeError(BridgeErrorCode.INVALID_STATE_TRANSITION, "Batch must be applied before commit")
        self._require_all(bundle, after=True)
        self._cleanup_all_temps(bundle)
        actual = self.workspace.compute_state_hash()
        if actual != bundle.record.workspace_state_hash_after:
            self._block(bundle, "Workspace hash differs before batch commit")
        record = self.journal.commit_multi_file_patch(command_id)
        return MultiFileRecoveryOutcome(
            command_id=record.command_id,
            state=record.state,
            action="committed",
            path_count=record.path_count,
            workspace_revision=record.workspace_revision_after or record.workspace_revision_before,
            workspace_state_hash=record.workspace_state_hash_after,
        )

    def recover(self, command_id: str) -> MultiFileRecoveryOutcome:
        bundle = self._require_bundle(command_id)
        state = bundle.record.state
        if state is MultiFileCheckpointState.PLANNED:
            self._require_all(bundle, after=False)
            self._cleanup_all_temps(bundle)
            return self._outcome(bundle, "ready_to_apply")
        if state is MultiFileCheckpointState.APPLYING:
            return self.apply(command_id)
        if state is MultiFileCheckpointState.APPLIED:
            self._require_all(bundle, after=True)
            self._cleanup_all_temps(bundle)
            return self._outcome(bundle, "awaiting_commit_or_rollback")
        if state is MultiFileCheckpointState.ROLLING_BACK:
            return self.rollback(command_id)
        if state is MultiFileCheckpointState.ROLLED_BACK:
            self._require_all(bundle, after=False)
            return self._outcome(bundle, "already_rolled_back")
        if state is MultiFileCheckpointState.COMMITTED:
            self._require_all(bundle, after=True)
            return self._outcome(bundle, "already_committed")
        return self._outcome(bundle, "blocked")

    def recover_all(self) -> tuple[MultiFileRecoveryOutcome, ...]:
        return tuple(
            self.recover(record.command_id)
            for record in self.journal.list_incomplete_multi_file_patch_checkpoints()
        )

    def _require_bundle(self, command_id: str) -> MultiFileCheckpointBundle:
        bundle = self.journal.get_multi_file_patch_bundle(command_id)
        if bundle is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Multi-file checkpoint not found")
        return bundle

    def _classify(self, item: MultiFileCheckpointPath) -> str:
        path = self.workspace.resolve_allowed_path(item.path)
        if path.is_symlink():
            return "unexpected"
        if not path.exists():
            current: bytes | None = None
        elif not path.is_file():
            return "unexpected"
        else:
            try:
                current = path.read_bytes()
            except OSError:
                return "unexpected"
        if item.before_exists == (current is not None) and item.before == current:
            return "before"
        if item.after_exists == (current is not None) and item.after == current:
            return "after"
        return "unexpected"

    def _require_all(self, bundle: MultiFileCheckpointBundle, *, after: bool) -> None:
        expected = "after" if after else "before"
        unexpected = [item.path for item in bundle.paths if self._classify(item) != expected]
        if unexpected:
            self._block(bundle, f"Checkpoint paths are not all in {expected} state: {unexpected[:10]}")

    def _write_exact(
        self,
        bundle: MultiFileCheckpointBundle,
        item: MultiFileCheckpointPath,
        *,
        expected: bytes | None,
        target: bytes,
        mode: str,
        fault_hook: FaultHook | None,
    ) -> None:
        path = self.workspace.resolve_allowed_path(item.path)
        if not path.parent.is_dir():
            self._block(bundle, f"Parent directory disappeared: {item.path}")
        current = None if not path.exists() else path.read_bytes() if path.is_file() and not path.is_symlink() else b"<invalid>"
        if current != expected:
            self._block(bundle, f"Path changed before atomic write: {item.path}")
        temp = self._temp_path(bundle, item, mode)
        if temp.exists() or temp.is_symlink():
            if not temp.is_file() or temp.is_symlink() or temp.read_bytes() != target:
                self._block(bundle, f"Unexpected checkpoint temp artifact: {temp.name}")
        else:
            with temp.open("xb") as stream:
                stream.write(target)
                stream.flush()
                os.fsync(stream.fileno())
            if temp.read_bytes() != target:
                raise OSError("temp reread mismatch")
            if fault_hook:
                fault_hook(f"AFTER_BATCH_TEMP_WRITTEN:{item.ordinal}:{mode}")
        current = None if not path.exists() else path.read_bytes() if path.is_file() and not path.is_symlink() else b"<invalid>"
        if current != expected:
            self._block(bundle, f"Path raced before promotion: {item.path}")
        if expected is None:
            os.link(temp, path)
            temp.unlink()
        else:
            os.replace(temp, path)
        WorkspaceManager._fsync_parent(path.parent)
        if not path.is_file() or path.is_symlink() or path.read_bytes() != target:
            self._block(bundle, f"Path differs after atomic write: {item.path}")

    def _delete_exact(self, item: MultiFileCheckpointPath, *, expected: bytes | None) -> None:
        path = self.workspace.resolve_allowed_path(item.path)
        if expected is None:
            if path.exists() or path.is_symlink():
                raise BridgeError(BridgeErrorCode.STATE_MISMATCH, f"Expected absent path: {item.path}")
            return
        if not path.is_file() or path.is_symlink() or path.read_bytes() != expected:
            raise BridgeError(BridgeErrorCode.STATE_MISMATCH, f"Path changed before delete: {item.path}")
        path.unlink()
        WorkspaceManager._fsync_parent(path.parent)
        if path.exists() or path.is_symlink():
            raise OSError("path still exists after delete")

    def _temp_path(
        self,
        bundle: MultiFileCheckpointBundle,
        item: MultiFileCheckpointPath,
        mode: str,
    ) -> Path:
        target = self.workspace.resolve_allowed_path(item.path)
        suffix = bundle.record.checkpoint_sha256[7:23]
        return target.parent / f".bdb_batch_{target.name}_{suffix}_{item.ordinal}_{mode}"

    def _cleanup_temps(self, bundle: MultiFileCheckpointBundle, item: MultiFileCheckpointPath) -> None:
        allowed = {item.before, item.after}
        for mode in ("apply", "rollback"):
            temp = self._temp_path(bundle, item, mode)
            if not (temp.exists() or temp.is_symlink()):
                continue
            if not temp.is_file() or temp.is_symlink():
                self._block(bundle, f"Checkpoint temp is not a regular file: {temp.name}")
            content = temp.read_bytes()
            if content not in allowed:
                self._block(bundle, f"Checkpoint temp bytes are unexpected: {temp.name}")
            temp.unlink()
            WorkspaceManager._fsync_parent(temp.parent)

    def _cleanup_all_temps(self, bundle: MultiFileCheckpointBundle) -> None:
        for item in bundle.paths:
            self._cleanup_temps(bundle, item)

    def _predicted_state_hash(
        self,
        paths: tuple[MultiFileCheckpointPath, ...],
        *,
        after: bool,
    ) -> str:
        overrides = {item.path: item.after if after else item.before for item in paths}
        head = self.workspace.git.run(["rev-parse", "HEAD"]).stdout.strip()
        changed = set(
            self.workspace.git.run(["ls-files", "-m", "-o", "--exclude-standard"]).stdout.splitlines()
        )
        changed.update(overrides)
        digest = hashlib.sha256()
        digest.update(b"bdb-poc-state-v1\0")
        digest.update(head.encode("ascii"))
        digest.update(b"\0")
        for relative in sorted(changed):
            if not self.workspace.is_allowed_path(relative):
                continue
            normalized = validate_repo_relative_path(relative)
            digest.update(normalized.encode("utf-8"))
            digest.update(b"\0")
            if normalized in overrides:
                content = overrides[normalized]
                digest.update(hashlib.sha256(content).digest() if content is not None else b"<missing>")
            else:
                path = self.workspace.resolve_allowed_path(normalized)
                digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"<missing>")
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def _block(self, bundle: MultiFileCheckpointBundle, diagnostic: str) -> None:
        self.journal.block_multi_file_patch(bundle.record.command_id, diagnostic)
        raise BridgeError(BridgeErrorCode.MANUAL_RECONCILIATION_REQUIRED, diagnostic)

    def _outcome(self, bundle: MultiFileCheckpointBundle, action: str) -> MultiFileRecoveryOutcome:
        record = self.journal.get_multi_file_patch_checkpoint(bundle.record.command_id) or bundle.record
        workspace = self.journal.get_workspace(record.session_id)
        if workspace is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Workspace disappeared")
        return MultiFileRecoveryOutcome(
            command_id=record.command_id,
            state=record.state,
            action=action,
            path_count=record.path_count,
            workspace_revision=workspace.revision,
            workspace_state_hash=workspace.state_hash,
        )
