from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from bdb_operator import OperatorApi, OperatorResponse

from .manifest import MUTATION_OPERATIONS, READ_OPERATIONS


ADAPTER_REQUEST_SCHEMA = "bdb-bartosz-os-request-v1"
ADAPTER_RESPONSE_SCHEMA = "bdb-bartosz-os-response-v1"
_ALL_OPERATIONS = frozenset((*READ_OPERATIONS, *MUTATION_OPERATIONS))


class AdapterOperator(Protocol):
    def capabilities(self) -> OperatorResponse: ...
    def list_projects(self, workspaces_root: str) -> OperatorResponse: ...
    def status(self, workspace_root: str) -> OperatorResponse: ...
    def events(self, workspace_root: str, **kwargs: Any) -> OperatorResponse: ...
    def current_operation(self, workspace_root: str) -> OperatorResponse: ...
    def logs(self, workspace_root: str, **kwargs: Any) -> OperatorResponse: ...
    def prepare(self, workspace_root: str, **kwargs: Any) -> OperatorResponse: ...
    def start(self, workspace_root: str, **kwargs: Any) -> OperatorResponse: ...
    def stop(self, workspace_root: str) -> OperatorResponse: ...
    def rearm(self, workspace_root: str, **kwargs: Any) -> OperatorResponse: ...


@dataclass(frozen=True)
class BartoszOsRequest:
    operation: str
    parameters: dict[str, Any] = field(default_factory=dict)
    mutation_authorized: bool = False
    request_id: str = field(default_factory=lambda: str(uuid4()))
    schema: str = ADAPTER_REQUEST_SCHEMA

    def validate(self) -> None:
        if self.schema != ADAPTER_REQUEST_SCHEMA:
            raise ValueError("unsupported adapter request schema")
        try:
            UUID(self.request_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("request_id must be a UUID") from error
        if self.operation not in _ALL_OPERATIONS:
            raise ValueError("operation is outside the closed adapter catalog")
        if not isinstance(self.parameters, dict):
            raise ValueError("parameters must be an object")
        if not isinstance(self.mutation_authorized, bool):
            raise ValueError("mutation_authorized must be boolean")

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "BartoszOsRequest":
        if not isinstance(document, Mapping):
            raise ValueError("adapter request must be an object")
        expected = {"schema", "request_id", "operation", "parameters", "mutation_authorized"}
        if set(document) != expected:
            raise ValueError("adapter request fields do not match v1")
        request = cls(
            schema=_string(document, "schema"),
            request_id=_string(document, "request_id"),
            operation=_string(document, "operation"),
            parameters=_object(document, "parameters"),
            mutation_authorized=_boolean(document, "mutation_authorized"),
        )
        request.validate()
        return request

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "operation": self.operation,
            "parameters": dict(self.parameters),
            "mutation_authorized": self.mutation_authorized,
        }


@dataclass(frozen=True)
class BartoszOsResponse:
    request_id: str
    operation: str
    ok: bool
    operator_response: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    schema: str = ADAPTER_RESPONSE_SCHEMA
    adapter_persisted_state: bool = False
    network_listener: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "operation": self.operation,
            "ok": self.ok,
            "adapter_persisted_state": self.adapter_persisted_state,
            "network_listener": self.network_listener,
            "operator_response": self.operator_response,
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }


class BartoszOsAdapter:
    """In-process, stateless adapter over the existing public Operator API."""

    def __init__(
        self,
        operator: AdapterOperator | None = None,
        *,
        mutations_enabled: bool = False,
    ) -> None:
        self._operator = operator or OperatorApi()
        self._mutations_enabled = bool(mutations_enabled)

    @property
    def mutations_enabled(self) -> bool:
        return self._mutations_enabled

    def handle(self, request: BartoszOsRequest | Mapping[str, Any]) -> BartoszOsResponse:
        try:
            parsed = request if isinstance(request, BartoszOsRequest) else BartoszOsRequest.from_dict(request)
            parsed.validate()
            if parsed.operation in MUTATION_OPERATIONS:
                if not self._mutations_enabled:
                    return self._failure(parsed, "mutation_adapter_disabled", "Mutation forwarding is disabled")
                if not parsed.mutation_authorized:
                    return self._failure(parsed, "mutation_authorization_required", "Explicit mutation authorization is required")
            elif parsed.mutation_authorized:
                return self._failure(parsed, "unexpected_mutation_authorization", "Read operations must not carry mutation authorization")
            response = self._dispatch(parsed)
            return BartoszOsResponse(
                request_id=parsed.request_id,
                operation=parsed.operation,
                ok=True,
                operator_response=response.to_dict(),
            )
        except (TypeError, ValueError) as error:
            request_id = getattr(request, "request_id", None)
            operation = getattr(request, "operation", None)
            if isinstance(request, Mapping):
                request_id = request.get("request_id")
                operation = request.get("operation")
            return BartoszOsResponse(
                request_id=request_id if isinstance(request_id, str) else "invalid-request",
                operation=operation if isinstance(operation, str) else "invalid-request",
                ok=False,
                error_code="invalid_adapter_request",
                error_message=str(error),
            )
        except Exception as error:  # defensive adapter boundary
            return BartoszOsResponse(
                request_id=getattr(request, "request_id", "adapter-internal"),
                operation=getattr(request, "operation", "adapter-internal"),
                ok=False,
                error_code="adapter_internal_error",
                error_message=f"{type(error).__name__}: {error}",
            )

    def _dispatch(self, request: BartoszOsRequest) -> OperatorResponse:
        operation = request.operation
        params = dict(request.parameters)
        if operation == "capabilities":
            _require_keys(params, set())
            return self._operator.capabilities()
        if operation == "list_projects":
            _require_keys(params, {"workspaces_root"})
            return self._operator.list_projects(_path(params, "workspaces_root"))
        if operation in {"status", "current_operation", "stop"}:
            _require_keys(params, {"workspace_root"})
            root = _path(params, "workspace_root")
            return getattr(self._operator, operation)(root)
        if operation == "events":
            _require_keys(
                params,
                {"workspace_root"},
                {"after_event_id", "limit", "session_id", "command_id"},
            )
            root = _path(params, "workspace_root")
            kwargs = {key: params[key] for key in ("after_event_id", "limit", "session_id", "command_id") if key in params}
            return self._operator.events(root, **kwargs)
        if operation == "logs":
            _require_keys(params, {"workspace_root"}, {"max_bytes", "max_lines"})
            root = _path(params, "workspace_root")
            kwargs = {key: params[key] for key in ("max_bytes", "max_lines") if key in params}
            return self._operator.logs(root, **kwargs)
        if operation in {"start", "rearm"}:
            _require_keys(params, {"workspace_root"}, {"arm_minutes"})
            root = _path(params, "workspace_root")
            kwargs = {"arm_minutes": params["arm_minutes"]} if "arm_minutes" in params else {}
            return getattr(self._operator, operation)(root, **kwargs)
        if operation == "prepare":
            _require_keys(
                params,
                {"workspace_root", "source_repo", "alias", "allowed_paths"},
                {"test_timeout_seconds", "python_executable"},
            )
            kwargs = {
                "source_repo": _path(params, "source_repo"),
                "alias": _text(params, "alias"),
                "allowed_paths": _string_list(params, "allowed_paths"),
            }
            for key in ("test_timeout_seconds", "python_executable"):
                if key in params:
                    kwargs[key] = params[key]
            return self._operator.prepare(_path(params, "workspace_root"), **kwargs)
        raise ValueError("operation is outside the closed adapter catalog")

    def _failure(self, request: BartoszOsRequest, code: str, message: str) -> BartoszOsResponse:
        return BartoszOsResponse(
            request_id=request.request_id,
            operation=request.operation,
            ok=False,
            error_code=code,
            error_message=message,
        )


def _require_keys(values: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = required - set(values)
    unexpected = set(values) - required - optional
    if missing:
        raise ValueError(f"missing parameters: {', '.join(sorted(missing))}")
    if unexpected:
        raise ValueError(f"unexpected parameters: {', '.join(sorted(unexpected))}")


def _path(values: Mapping[str, Any], key: str) -> str:
    return _text(values, key)


def _text(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _string_list(values: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = values.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{key} must be a non-empty string array")
    return tuple(item.strip() for item in value)


def _string(document: Mapping[str, Any], key: str) -> str:
    return _text(document, key)


def _object(document: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


def _boolean(document: Mapping[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value
