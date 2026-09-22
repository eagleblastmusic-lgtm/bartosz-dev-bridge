"""Canonical Project Memory projection for Browser project-launch transport.

The Browser queue is only a transport projection. Rich vNext launches carry
just enough immutable identity to prove which canonical execution binding and
outbox record they belong to. This module fail-closes partial or conflicting
metadata, reactivates a legitimately retried blocked task when the Browser
actually claims it, and records Browser acknowledgement before the transport
projection may disappear.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .project_execution import (
    OUTBOX_STATUS_ACKNOWLEDGED,
    OUTBOX_STATUS_PENDING,
    OUTBOX_STATUS_PUBLISHED,
    ProjectExecutionCoordinator,
)
from .project_launch import ProjectLaunch
from .project_memory import ProjectMemoryState, ProjectMemoryStore


_CONVERSATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_CANONICAL_METADATA_FIELDS = (
    "project_id",
    "plan_version",
    "task_id",
    "execution_binding_id",
    "correlation_id",
    "command_id",
    "expected_repo_head_before",
)
_ALLOWED_CLAIM_TASK_STATUSES = frozenset({"pending", "active", "blocked"})
_TERMINAL_TASK_STATUSES = frozenset({"completed", "skipped"})


class ProjectLaunchCanonicalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProjectLaunchCanonicalError(code, message)


def _conversation(value: object) -> str:
    if not isinstance(value, str) or _CONVERSATION_RE.fullmatch(value) is None:
        _fail("project_launch_conversation_invalid", "conversation_id has an unsafe format")
    return value


class ProjectLaunchCanonicalState:
    """Bridge rich launch transport state to canonical vNext Project Memory."""

    def __init__(
        self,
        runtime_root: str | Path,
        *,
        execution: ProjectExecutionCoordinator | None = None,
        memory_factory: Callable[[str], ProjectMemoryStore] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).expanduser().absolute()
        self.execution = execution or ProjectExecutionCoordinator(self.runtime_root)
        self._memory_factory = memory_factory or (
            lambda project_id: ProjectMemoryStore(self.runtime_root, project_id)
        )

    @staticmethod
    def is_canonical_launch(launch: ProjectLaunch) -> bool:
        present = [getattr(launch, field) is not None for field in _CANONICAL_METADATA_FIELDS]
        if not any(present):
            return False
        if not all(present):
            _fail(
                "project_launch_metadata_incomplete",
                "rich project launch metadata is incomplete",
            )
        return True

    @staticmethod
    def _identity(launch: ProjectLaunch) -> dict[str, str]:
        if not ProjectLaunchCanonicalState.is_canonical_launch(launch):
            _fail("project_launch_legacy", "legacy project launch has no canonical identity")
        return {
            "project_id": str(launch.project_id),
            "plan_version": str(launch.plan_version),
            "task_id": str(launch.task_id),
            "execution_binding_id": str(launch.execution_binding_id),
            "launch_id": launch.launch_id,
            "correlation_id": str(launch.correlation_id),
            "command_id": str(launch.command_id),
            "repo_alias": launch.repo_alias,
            "expected_repo_head_before": str(launch.expected_repo_head_before),
        }

    @staticmethod
    def _require_equal(actual: object, expected: str, field: str) -> None:
        if str(actual) != expected:
            _fail(
                "project_launch_identity_mismatch",
                f"canonical {field} differs from queued launch identity",
            )

    def _validate_outbox(self, launch: ProjectLaunch):
        identity = self._identity(launch)
        outbox = self.execution.launch_outbox_record(identity["project_id"], launch.launch_id)
        if outbox is None:
            _fail("project_launch_outbox_missing", "canonical launch outbox record is missing")
        if outbox.prompt != launch.prompt or outbox.auto_send != launch.auto_send:
            _fail("project_launch_payload_mismatch", "queued prompt or delivery mode differs from canonical outbox")
        for field in (
            "project_id",
            "plan_version",
            "task_id",
            "execution_binding_id",
            "correlation_id",
            "command_id",
            "repo_alias",
            "expected_repo_head_before",
        ):
            self._require_equal(getattr(outbox, field), identity[field], f"outbox.{field}")
        if outbox.status not in {
            OUTBOX_STATUS_PENDING,
            OUTBOX_STATUS_PUBLISHED,
            OUTBOX_STATUS_ACKNOWLEDGED,
        }:
            _fail("project_launch_outbox_invalid", "canonical launch outbox status is unsupported")
        return identity, outbox

    def _validate_binding(self, launch: ProjectLaunch):
        identity, outbox = self._validate_outbox(launch)
        binding = self.execution.binding(
            identity["project_id"], identity["execution_binding_id"]
        )
        for field in (
            "project_id",
            "plan_version",
            "task_id",
            "execution_binding_id",
            "launch_id",
            "correlation_id",
            "command_id",
            "repo_alias",
            "expected_repo_head_before",
        ):
            self._require_equal(getattr(binding, field), identity[field], f"binding.{field}")
        if binding.status != "ACTIVE" or binding.superseded:
            _fail("project_launch_binding_stale", "canonical execution binding is not active")
        snapshot = self.execution.snapshot(identity["project_id"])
        if snapshot.get("current_binding_id") != identity["execution_binding_id"]:
            _fail("project_launch_binding_stale", "queued launch is not the current canonical binding")
        if snapshot.get("current_task_id") not in (None, identity["task_id"]):
            _fail("project_launch_task_stale", "queued launch is not the current canonical task")
        return identity, outbox, binding, snapshot

    def is_acknowledged(self, launch: ProjectLaunch) -> bool:
        """Return durable ACK state without requiring the binding to remain active."""
        if not self.is_canonical_launch(launch):
            return False
        _identity, outbox = self._validate_outbox(launch)
        return outbox.status == OUTBOX_STATUS_ACKNOWLEDGED

    def activate_claimed(self, launch: ProjectLaunch) -> None:
        """Reactivate exactly the current retry binding when Browser owns its lease."""
        identity, outbox, _binding, snapshot = self._validate_binding(launch)
        if outbox.status == OUTBOX_STATUS_ACKNOWLEDGED:
            _fail("project_launch_already_acknowledged", "launch was already acknowledged")
        task_status = snapshot.get("task_statuses", {}).get(identity["task_id"], "pending")
        if task_status in _TERMINAL_TASK_STATUSES:
            _fail("project_launch_task_terminal", "completed task cannot be reactivated")
        if task_status not in _ALLOWED_CLAIM_TASK_STATUSES:
            _fail(
                "project_launch_task_state_invalid",
                f"task status '{task_status}' cannot be activated by a Browser claim",
            )

        memory = self._memory_factory(identity["project_id"])

        def transition(state: ProjectMemoryState) -> tuple[ProjectMemoryState, None]:
            if not isinstance(state.execution, Mapping):
                _fail("project_launch_execution_missing", "canonical execution document is missing")
            execution = dict(state.execution)
            bindings = execution.get("bindings")
            if not isinstance(bindings, list):
                _fail("project_launch_execution_invalid", "canonical binding collection is invalid")
            raw_binding = next(
                (
                    item
                    for item in bindings
                    if isinstance(item, Mapping)
                    and item.get("execution_binding_id") == identity["execution_binding_id"]
                ),
                None,
            )
            if raw_binding is None:
                _fail("project_launch_binding_missing", "canonical execution binding is missing")
            for field in (
                "project_id",
                "plan_version",
                "task_id",
                "execution_binding_id",
                "launch_id",
                "correlation_id",
                "command_id",
                "repo_alias",
                "expected_repo_head_before",
            ):
                self._require_equal(raw_binding.get(field), identity[field], f"binding.{field}")
            if raw_binding.get("status") != "ACTIVE" or raw_binding.get("superseded") is True:
                _fail("project_launch_binding_stale", "canonical execution binding is not active")
            if execution.get("current_binding_id") != identity["execution_binding_id"]:
                _fail("project_launch_binding_stale", "queued launch is no longer current")

            raw_outboxes = execution.get("launch_outbox")
            if not isinstance(raw_outboxes, Mapping):
                _fail("project_launch_outbox_missing", "canonical launch outbox collection is missing")
            raw_outbox = raw_outboxes.get(identity["launch_id"])
            if not isinstance(raw_outbox, Mapping):
                _fail("project_launch_outbox_missing", "canonical launch outbox record is missing")
            if raw_outbox.get("prompt") != launch.prompt or raw_outbox.get("auto_send") != launch.auto_send:
                _fail("project_launch_payload_mismatch", "queued payload changed before claim activation")
            for field in (
                "project_id",
                "plan_version",
                "task_id",
                "execution_binding_id",
                "correlation_id",
                "command_id",
                "repo_alias",
                "expected_repo_head_before",
            ):
                self._require_equal(raw_outbox.get(field), identity[field], f"outbox.{field}")
            if raw_outbox.get("status") == OUTBOX_STATUS_ACKNOWLEDGED:
                _fail("project_launch_already_acknowledged", "launch was already acknowledged")

            statuses = dict(execution.get("task_statuses", {}))
            current_status = statuses.get(identity["task_id"], "pending")
            if current_status in _TERMINAL_TASK_STATUSES:
                _fail("project_launch_task_terminal", "completed task cannot be reactivated")
            if current_status not in _ALLOWED_CLAIM_TASK_STATUSES:
                _fail(
                    "project_launch_task_state_invalid",
                    f"task status '{current_status}' cannot be activated by a Browser claim",
                )
            statuses[identity["task_id"]] = "active"
            execution["task_statuses"] = statuses
            execution["current_task_id"] = identity["task_id"]
            execution["current_binding_id"] = identity["execution_binding_id"]
            return replace(state, execution=execution), None

        memory.execution_transaction(transition)

    def acknowledge_delivery(self, launch: ProjectLaunch, conversation_id: str) -> None:
        """Durably bind Browser conversation, then ACK the canonical outbox.

        The two Project Memory transitions are intentionally ordered. If the
        process stops after conversation binding but before outbox ACK, the
        transport lease remains and retry is idempotent. A bound conversation
        also prevents unsafe operator replay into another conversation.
        """
        conversation = _conversation(conversation_id)
        identity, _outbox, _binding, _snapshot = self._validate_binding(launch)
        self.execution.bind_conversation(
            identity["project_id"], identity["execution_binding_id"], conversation
        )
        # Re-check canonical current ownership after the first durable write.
        snapshot = self.execution.snapshot(identity["project_id"])
        if snapshot.get("current_binding_id") != identity["execution_binding_id"]:
            _fail("project_launch_binding_stale", "binding changed before launch acknowledgement")
        _identity, current_outbox = self._validate_outbox(launch)
        if current_outbox.status != OUTBOX_STATUS_ACKNOWLEDGED:
            self.execution.mark_outbox_acknowledged(
                identity["project_id"], launch.launch_id, conversation_id=conversation
            )


__all__ = [
    "ProjectLaunchCanonicalError",
    "ProjectLaunchCanonicalState",
]
