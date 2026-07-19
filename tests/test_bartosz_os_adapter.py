from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from bdb_bartosz_os import BartoszOsAdapter, BartoszOsRequest
from bdb_operator.models import OperatorError, OperatorResponse


class FakeOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail_status = False

    def capabilities(self) -> OperatorResponse:
        self.calls.append(("capabilities",))
        return OperatorResponse.success("capabilities", data={"transport": "in_process"})

    def list_projects(self, workspaces_root: str) -> OperatorResponse:
        self.calls.append(("list_projects", workspaces_root))
        return OperatorResponse.success("list_projects", data={"projects": []})

    def status(self, workspace_root: str) -> OperatorResponse:
        self.calls.append(("status", workspace_root))
        if self.fail_status:
            return OperatorResponse.failure(
                "status",
                error=OperatorError(code="workspace_missing", message="missing"),
            )
        return OperatorResponse.success("status", data={"status": "READY"})

    def events(self, workspace_root: str, **kwargs: object) -> OperatorResponse:
        self.calls.append(("events", workspace_root, kwargs))
        return OperatorResponse.success("events", data={"events": []})

    def current_operation(self, workspace_root: str) -> OperatorResponse:
        self.calls.append(("current_operation", workspace_root))
        return OperatorResponse.success("current_operation", data={"active": False})

    def logs(self, workspace_root: str, **kwargs: object) -> OperatorResponse:
        self.calls.append(("logs", workspace_root, kwargs))
        return OperatorResponse.success("logs", data={"sources": []})

    def prepare(self, workspace_root: str, **kwargs: object) -> OperatorResponse:
        self.calls.append(("prepare", workspace_root, kwargs))
        return OperatorResponse.success(
            "prepare",
            project_alias=str(kwargs["alias"]),
            data={"status": "prepared"},
        )

    def start(self, workspace_root: str, **kwargs: object) -> OperatorResponse:
        self.calls.append(("start", workspace_root, kwargs))
        return OperatorResponse.success("start", data={"status": "RUNNING"})

    def stop(self, workspace_root: str) -> OperatorResponse:
        self.calls.append(("stop", workspace_root))
        return OperatorResponse.success("stop", data={"status": "OFFLINE"})

    def rearm(self, workspace_root: str, **kwargs: object) -> OperatorResponse:
        self.calls.append(("rearm", workspace_root, kwargs))
        return OperatorResponse.success("rearm", data={"armed": True})


def request(operation: str, parameters: dict[str, object], *, authorized: bool = False) -> BartoszOsRequest:
    return BartoszOsRequest(
        request_id=str(uuid4()),
        operation=operation,
        parameters=parameters,
        mutation_authorized=authorized,
    )


def test_request_round_trip_preserves_closed_v1_document() -> None:
    original = request("events", {"workspace_root": "C:/w", "limit": 25})

    restored = BartoszOsRequest.from_dict(original.to_dict())

    assert restored == original
    assert set(restored.to_dict()) == {
        "schema",
        "request_id",
        "operation",
        "parameters",
        "mutation_authorized",
    }


def test_read_operation_routes_without_mutation_enablement(tmp_path: Path) -> None:
    operator = FakeOperator()
    adapter = BartoszOsAdapter(operator)

    response = adapter.handle(request("status", {"workspace_root": str(tmp_path)}))

    assert response.ok is True
    assert response.adapter_persisted_state is False
    assert response.network_listener is False
    assert response.operator_response is not None
    assert response.operator_response["operation"] == "status"
    assert operator.calls == [("status", str(tmp_path))]


def test_mutation_is_disabled_by_default() -> None:
    operator = FakeOperator()
    adapter = BartoszOsAdapter(operator)

    response = adapter.handle(request("start", {"workspace_root": "C:/w"}, authorized=True))

    assert response.ok is False
    assert response.error_code == "mutation_adapter_disabled"
    assert operator.calls == []


def test_enabled_adapter_still_requires_request_authorization() -> None:
    operator = FakeOperator()
    adapter = BartoszOsAdapter(operator, mutations_enabled=True)

    response = adapter.handle(request("stop", {"workspace_root": "C:/w"}))

    assert response.ok is False
    assert response.error_code == "mutation_authorization_required"
    assert operator.calls == []


def test_authorized_start_routes_exact_closed_parameters() -> None:
    operator = FakeOperator()
    adapter = BartoszOsAdapter(operator, mutations_enabled=True)

    response = adapter.handle(
        request("start", {"workspace_root": "C:/w", "arm_minutes": 17}, authorized=True)
    )

    assert response.ok is True
    assert operator.calls == [("start", "C:/w", {"arm_minutes": 17})]


def test_authorized_prepare_routes_exact_public_contract() -> None:
    operator = FakeOperator()
    adapter = BartoszOsAdapter(operator, mutations_enabled=True)
    parameters = {
        "workspace_root": "C:/workspaces/alpha",
        "source_repo": "C:/source/alpha",
        "alias": "alpha",
        "allowed_paths": ["README.md", "tests/*.py"],
        "test_timeout_seconds": 90,
        "python_executable": "C:/Python/python.exe",
    }

    response = adapter.handle(request("prepare", parameters, authorized=True))

    assert response.ok is True
    assert operator.calls == [
        (
            "prepare",
            "C:/workspaces/alpha",
            {
                "source_repo": "C:/source/alpha",
                "alias": "alpha",
                "allowed_paths": ("README.md", "tests/*.py"),
                "test_timeout_seconds": 90,
                "python_executable": "C:/Python/python.exe",
            },
        )
    ]


def test_unknown_parameter_is_rejected_before_operator() -> None:
    operator = FakeOperator()
    response = BartoszOsAdapter(operator).handle(
        request("status", {"workspace_root": "C:/w", "unexpected": True})
    )

    assert response.ok is False
    assert response.error_code == "invalid_adapter_request"
    assert "unexpected parameters" in (response.error_message or "")
    assert operator.calls == []


def test_read_request_must_not_carry_mutation_authorization() -> None:
    operator = FakeOperator()
    response = BartoszOsAdapter(operator).handle(
        request("capabilities", {}, authorized=True)
    )

    assert response.ok is False
    assert response.error_code == "unexpected_mutation_authorization"
    assert operator.calls == []


def test_operator_failure_is_forwarded_without_becoming_adapter_state() -> None:
    operator = FakeOperator()
    operator.fail_status = True
    response = BartoszOsAdapter(operator).handle(
        request("status", {"workspace_root": "C:/missing"})
    )

    assert response.ok is True
    assert response.operator_response is not None
    assert response.operator_response["ok"] is False
    assert response.operator_response["error"]["code"] == "workspace_missing"
    assert response.adapter_persisted_state is False


def test_mapping_request_requires_closed_fields_and_uuid() -> None:
    adapter = BartoszOsAdapter(FakeOperator())
    invalid = {
        "schema": "bdb-bartosz-os-request-v1",
        "request_id": "not-a-uuid",
        "operation": "capabilities",
        "parameters": {},
        "mutation_authorized": False,
    }

    response = adapter.handle(invalid)

    assert response.ok is False
    assert response.error_code == "invalid_adapter_request"
