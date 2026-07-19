from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bdb_operator import OperatorApi, OperatorResponse


GUI_SESSION_HISTORY_SCHEMA = "bdb-gui-session-history-v1"
GUI_SESSION_SCHEMA = "bdb-gui-session-summary-v1"
GUI_SESSION_ATTEMPT_SCHEMA = "bdb-gui-session-attempt-v1"
GUI_REPAIR_GROUP_SCHEMA = "bdb-gui-repair-group-v1"
REPAIR_CORRELATION_SCHEMA = "bdb-repair-correlation-v1"
MAX_SESSION_HISTORY_LIMIT = 100


class SessionHistoryOperator(Protocol):
    def sessions(self, workspace_root: str | Path, *, limit: int = 20) -> OperatorResponse:
        ...


@dataclass(frozen=True)
class SessionFileState:
    path: str | None
    exists: bool
    valid: bool
    warning: str | None

    @classmethod
    def from_document(cls, path: Any, document: dict[str, Any], label: str) -> "SessionFileState":
        normalized_path = _optional_string(path, f"{label}_path")
        exists = _required_bool(document, "exists")
        valid = _required_bool(document, "valid")
        warning = _optional_string(document.get("warning"), "warning")
        if valid and (not exists or normalized_path is None):
            raise ValueError(f"Valid {label} requires an existing path")
        return cls(normalized_path, exists, valid, warning)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "valid": self.valid,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class PromotionReceiptSummary:
    status: str
    source_commit: str
    parent_commit: str
    changed_files: tuple[str, ...]
    promoted_at: str | None
    result_sha256: str | None

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PromotionReceiptSummary":
        return cls(
            status=_required_string(document, "status"),
            source_commit=_required_string(document, "source_commit"),
            parent_commit=_required_string(document, "parent_commit"),
            changed_files=_string_tuple(document.get("changed_files"), "changed_files"),
            promoted_at=_optional_string(document.get("promoted_at"), "promoted_at"),
            result_sha256=_optional_string(document.get("result_sha256"), "result_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_commit": self.source_commit,
            "parent_commit": self.parent_commit,
            "changed_files": list(self.changed_files),
            "promoted_at": self.promoted_at,
            "result_sha256": self.result_sha256,
        }


@dataclass(frozen=True)
class RepairCorrelationSummary:
    correlation_id: str
    role: str
    predecessor_session_id: str | None
    schema: str = REPAIR_CORRELATION_SCHEMA

    @classmethod
    def from_document(cls, document: dict[str, Any], *, session_id: str) -> "RepairCorrelationSummary":
        if document.get("schema") != REPAIR_CORRELATION_SCHEMA:
            raise ValueError("Repair correlation schema is unsupported")
        correlation_id = _required_string(document, "correlation_id")
        role = _required_string(document, "role")
        predecessor = _optional_string(document.get("predecessor_session_id"), "predecessor_session_id")
        if role == "initial":
            if predecessor is not None:
                raise ValueError("Initial repair correlation cannot have a predecessor")
        elif role == "repair":
            if predecessor is None or predecessor == session_id:
                raise ValueError("Repair correlation requires a distinct predecessor")
        else:
            raise ValueError("Repair correlation role is unsupported")
        return cls(correlation_id, role, predecessor)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "correlation_id": self.correlation_id,
            "role": self.role,
            "predecessor_session_id": self.predecessor_session_id,
        }


@dataclass(frozen=True)
class RepairGroupSummary:
    correlation_id: str
    verified: bool
    initial_session_id: str | None
    repair_session_ids: tuple[str, ...]
    session_ids: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    relationship_inferred: bool
    schema: str = GUI_REPAIR_GROUP_SCHEMA

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "RepairGroupSummary":
        if document.get("schema") != "bdb-repair-group-v1":
            raise ValueError("Operator repair group schema is unsupported")
        inferred = _required_bool(document, "relationship_inferred")
        if inferred:
            raise ValueError("Inferred repair relationships are forbidden")
        edges_raw = document.get("edges")
        if not isinstance(edges_raw, list):
            raise ValueError("Repair group edges must be an array")
        edges: list[tuple[str, str]] = []
        for item in edges_raw:
            edge = _object(item, "repair edge")
            edges.append(
                (
                    _required_string(edge, "predecessor_session_id"),
                    _required_string(edge, "repair_session_id"),
                )
            )
        verified = _required_bool(document, "verified")
        warnings = _string_tuple(document.get("warnings"), "warnings")
        if verified and warnings:
            raise ValueError("Verified repair group cannot contain warnings")
        return cls(
            correlation_id=_required_string(document, "correlation_id"),
            verified=verified,
            initial_session_id=_optional_string(document.get("initial_session_id"), "initial_session_id"),
            repair_session_ids=_string_tuple(document.get("repair_session_ids"), "repair_session_ids"),
            session_ids=_string_tuple(document.get("session_ids"), "session_ids"),
            edges=tuple(edges),
            warnings=warnings,
            relationship_inferred=inferred,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "correlation_id": self.correlation_id,
            "verified": self.verified,
            "initial_session_id": self.initial_session_id,
            "repair_session_ids": list(self.repair_session_ids),
            "session_ids": list(self.session_ids),
            "edges": [
                {
                    "predecessor_session_id": predecessor,
                    "repair_session_id": repair,
                }
                for predecessor, repair in self.edges
            ],
            "warnings": list(self.warnings),
            "relationship_inferred": self.relationship_inferred,
        }


@dataclass(frozen=True)
class SessionAttempt:
    command_id: str
    sequence: int
    command_state: str
    operation: str | None
    target_path: str | None
    profile_id: str | None
    created_at: str
    updated_at: str
    result_created_at: str | None
    result_status: str | None
    error_code: str | None
    exit_code: int | None
    checkpoint_state: str | None
    rollback_performed: bool | None
    changed_files: tuple[str, ...]
    result_sha256: str | None
    result_file: SessionFileState
    receipt_file: SessionFileState
    receipt: PromotionReceiptSummary | None
    warnings: tuple[str, ...]
    schema: str = GUI_SESSION_ATTEMPT_SCHEMA

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "SessionAttempt":
        if document.get("schema") != "bdb-session-attempt-v1":
            raise ValueError("Operator session attempt schema is unsupported")
        result = _object(document.get("result"), "result")
        receipt_raw = document.get("receipt")
        receipt = None if receipt_raw is None else PromotionReceiptSummary.from_document(_object(receipt_raw, "receipt"))
        result_file = SessionFileState.from_document(
            document.get("result_path"),
            _object(document.get("result_file"), "result_file"),
            "result",
        )
        receipt_file = SessionFileState.from_document(
            document.get("receipt_path"),
            _object(document.get("receipt_file"), "receipt_file"),
            "receipt",
        )
        if receipt is not None and not receipt_file.valid:
            raise ValueError("Receipt summary requires a valid receipt file")
        return cls(
            command_id=_required_string(document, "command_id"),
            sequence=_required_positive_int(document, "sequence"),
            command_state=_required_string(document, "command_state"),
            operation=_optional_string(document.get("operation"), "operation"),
            target_path=_optional_string(document.get("target_path"), "target_path"),
            profile_id=_optional_string(document.get("profile_id"), "profile_id"),
            created_at=_required_string(document, "created_at"),
            updated_at=_required_string(document, "updated_at"),
            result_created_at=_optional_string(document.get("result_created_at"), "result_created_at"),
            result_status=_optional_string(result.get("status"), "status"),
            error_code=_optional_string(result.get("error_code"), "error_code"),
            exit_code=_optional_int(result.get("exit_code"), "exit_code"),
            checkpoint_state=_optional_string(result.get("checkpoint_state"), "checkpoint_state"),
            rollback_performed=_optional_bool(result.get("rollback_performed"), "rollback_performed"),
            changed_files=_string_tuple(result.get("changed_files"), "changed_files"),
            result_sha256=_optional_string(result.get("result_sha256"), "result_sha256"),
            result_file=result_file,
            receipt_file=receipt_file,
            receipt=receipt,
            warnings=_string_tuple(document.get("warnings"), "warnings"),
        )

    @property
    def promotion_status(self) -> str:
        if self.receipt is not None:
            return self.receipt.status
        if self.receipt_file.exists and not self.receipt_file.valid:
            return "invalid_receipt"
        return "not_promoted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "command_id": self.command_id,
            "sequence": self.sequence,
            "command_state": self.command_state,
            "operation": self.operation,
            "target_path": self.target_path,
            "profile_id": self.profile_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result_created_at": self.result_created_at,
            "result": {
                "status": self.result_status,
                "error_code": self.error_code,
                "exit_code": self.exit_code,
                "checkpoint_state": self.checkpoint_state,
                "rollback_performed": self.rollback_performed,
                "changed_files": list(self.changed_files),
                "result_sha256": self.result_sha256,
            },
            "result_file": self.result_file.to_dict(),
            "receipt_file": self.receipt_file.to_dict(),
            "receipt": self.receipt.to_dict() if self.receipt is not None else None,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    repository_id: str
    base_sha: str
    state: str
    created_at: str
    updated_at: str
    workspace_path: str | None
    workspace_revision: int | None
    attempts: tuple[SessionAttempt, ...]
    attempts_truncated: bool
    repair_group_id: str | None
    repair_correlation: RepairCorrelationSummary | None
    repair_relationship_inferred: bool
    warnings: tuple[str, ...]
    schema: str = GUI_SESSION_SCHEMA

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "SessionSummary":
        if document.get("schema") != "bdb-session-summary-v1":
            raise ValueError("Operator session summary schema is unsupported")
        workspace = _object(document.get("workspace"), "workspace")
        attempts_raw = document.get("attempts")
        if not isinstance(attempts_raw, list):
            raise ValueError("Operator session attempts must be an array")
        attempts = tuple(SessionAttempt.from_document(_object(item, "attempt")) for item in attempts_raw)
        if document.get("attempt_count") != len(attempts):
            raise ValueError("Operator session attempt_count does not match attempts")
        inferred = _required_bool(document, "repair_relationship_inferred")
        if inferred:
            raise ValueError("Inferred repair relationships are forbidden")
        session_id = _required_string(document, "session_id")
        correlation_raw = document.get("repair_correlation")
        correlation = (
            None
            if correlation_raw is None
            else RepairCorrelationSummary.from_document(
                _object(correlation_raw, "repair_correlation"),
                session_id=session_id,
            )
        )
        repair_group = _optional_string(document.get("repair_group_id"), "repair_group_id")
        if (correlation is None and repair_group is not None) or (
            correlation is not None and repair_group != correlation.correlation_id
        ):
            raise ValueError("Session repair_group_id does not match explicit correlation")
        return cls(
            session_id=session_id,
            repository_id=_required_string(document, "repository_id"),
            base_sha=_required_string(document, "base_sha"),
            state=_required_string(document, "state"),
            created_at=_required_string(document, "created_at"),
            updated_at=_required_string(document, "updated_at"),
            workspace_path=_optional_string(workspace.get("path"), "workspace.path"),
            workspace_revision=_optional_int(workspace.get("revision"), "workspace.revision"),
            attempts=attempts,
            attempts_truncated=_required_bool(document, "attempts_truncated"),
            repair_group_id=repair_group,
            repair_correlation=correlation,
            repair_relationship_inferred=inferred,
            warnings=_string_tuple(document.get("warnings", []), "warnings"),
        )

    @property
    def latest_attempt(self) -> SessionAttempt | None:
        return self.attempts[-1] if self.attempts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "session_id": self.session_id,
            "repository_id": self.repository_id,
            "base_sha": self.base_sha,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace": {
                "path": self.workspace_path,
                "revision": self.workspace_revision,
            },
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "attempts_truncated": self.attempts_truncated,
            "repair_group_id": self.repair_group_id,
            "repair_correlation": self.repair_correlation.to_dict() if self.repair_correlation else None,
            "repair_relationship_inferred": self.repair_relationship_inferred,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class SessionHistorySnapshot:
    workspace_root: str
    project_alias: str | None
    generated_at: str | None
    sessions: tuple[SessionSummary, ...]
    repair_groups: tuple[RepairGroupSummary, ...]
    limit: int
    operator_operation_id: str
    error_code: str | None = None
    error_message: str | None = None
    read_only: bool = True
    repair_relationships_inferred: bool = False
    mutation_operations_invoked: int = 0
    schema: str = GUI_SESSION_HISTORY_SCHEMA

    @property
    def ok(self) -> bool:
        return self.error_code is None

    def group_for_session(self, session_id: str) -> RepairGroupSummary | None:
        for group in self.repair_groups:
            if session_id in group.session_ids:
                return group
        return None

    @classmethod
    def failure(
        cls,
        workspace_root: str | Path,
        *,
        operation_id: str,
        project_alias: str | None,
        error_code: str,
        error_message: str,
        limit: int,
    ) -> "SessionHistorySnapshot":
        return cls(
            workspace_root=_resolved(workspace_root),
            project_alias=project_alias,
            generated_at=None,
            sessions=(),
            repair_groups=(),
            limit=limit,
            operator_operation_id=operation_id,
            error_code=error_code,
            error_message=error_message,
        )

    @classmethod
    def from_response(
        cls,
        workspace_root: str | Path,
        response: OperatorResponse,
        *,
        requested_limit: int,
    ) -> "SessionHistorySnapshot":
        if not response.ok:
            return cls.failure(
                workspace_root,
                operation_id=response.operation_id,
                project_alias=response.project_alias,
                error_code=response.error.code if response.error is not None else "operator_error",
                error_message=response.error.message if response.error is not None else "Session history read failed",
                limit=requested_limit,
            )
        try:
            data = _object(response.data, "session history")
            if data.get("schema") != "bdb-session-history-v1":
                raise ValueError("Operator session history schema is unsupported")
            if data.get("read_only") is not True or data.get("repair_relationships_inferred") is not False:
                raise ValueError("Operator session history safety flags are invalid")
            if data.get("limit") != requested_limit:
                raise ValueError("Operator session history limit does not match the request")
            raw_sessions = data.get("sessions")
            if not isinstance(raw_sessions, list):
                raise ValueError("Operator sessions must be an array")
            sessions = tuple(SessionSummary.from_document(_object(item, "session")) for item in raw_sessions)
            raw_groups = data.get("repair_groups", [])
            if not isinstance(raw_groups, list):
                raise ValueError("Operator repair_groups must be an array")
            groups = tuple(
                RepairGroupSummary.from_document(_object(item, "repair_group"))
                for item in raw_groups
            )
            _validate_group_membership(sessions, groups)
            return cls(
                workspace_root=_resolved(workspace_root),
                project_alias=_optional_string(data.get("project_alias"), "project_alias") or response.project_alias,
                generated_at=_required_string(data, "generated_at"),
                sessions=sessions,
                repair_groups=groups,
                limit=requested_limit,
                operator_operation_id=response.operation_id,
            )
        except ValueError as error:
            return cls.failure(
                workspace_root,
                operation_id=response.operation_id,
                project_alias=response.project_alias,
                error_code="invalid_operator_response",
                error_message=str(error),
                limit=requested_limit,
            )


class SessionHistoryService:
    """Bounded GUI adapter over the read-only OperatorApi.sessions projection."""

    def __init__(self, operator: SessionHistoryOperator | None = None) -> None:
        self._operator = operator or OperatorApi()

    def read(self, workspace_root: str | Path, *, limit: int = 20) -> SessionHistorySnapshot:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SESSION_HISTORY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_SESSION_HISTORY_LIMIT}")
        response = self._operator.sessions(workspace_root, limit=limit)
        return SessionHistorySnapshot.from_response(workspace_root, response, requested_limit=limit)


def _validate_group_membership(
    sessions: tuple[SessionSummary, ...],
    groups: tuple[RepairGroupSummary, ...],
) -> None:
    by_id = {session.session_id: session for session in sessions}
    by_correlation = {group.correlation_id: group for group in groups}
    if len(by_correlation) != len(groups):
        raise ValueError("Repair group correlation IDs must be unique")
    for group in groups:
        if any(session_id not in by_id for session_id in group.session_ids):
            raise ValueError("Repair group references a session outside the bounded response")
        if group.initial_session_id is not None and group.initial_session_id not in group.session_ids:
            raise ValueError("Repair group initial session is not a group member")
        if any(session_id not in group.session_ids for session_id in group.repair_session_ids):
            raise ValueError("Repair group repair session is not a group member")
        for predecessor, repair in group.edges:
            if predecessor not in group.session_ids or repair not in group.session_ids:
                raise ValueError("Repair group edge references a non-member session")
    for session in sessions:
        correlation = session.repair_correlation
        if correlation is None:
            continue
        group = by_correlation.get(correlation.correlation_id)
        if group is None or session.session_id not in group.session_ids:
            raise ValueError("Explicit session correlation is missing its repair group")


def _resolved(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Operator {label} must be an object")
    return value


def _required_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value


def _optional_string(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Operator field has an invalid string type: {key}")
    return value


def _required_positive_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value


def _optional_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Operator field has an invalid integer type: {key}")
    return value


def _required_bool(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Operator field is missing or invalid: {key}")
    return value


def _optional_bool(value: Any, key: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Operator field has an invalid boolean type: {key}")
    return value


def _string_tuple(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Operator field must be a string array: {key}")
    return tuple(value)
