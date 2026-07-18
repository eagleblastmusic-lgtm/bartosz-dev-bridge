from __future__ import annotations

from pathlib import Path

from bdb_gui.bootstrap import BootstrapService
from bdb_gui.state import GUI_BOOTSTRAP_SCHEMA
from bdb_operator.models import OperatorError, OperatorResponse


class FakeBootstrapOperator:
    def __init__(
        self,
        *,
        capabilities: OperatorResponse,
        projects: OperatorResponse | None = None,
    ) -> None:
        self.capabilities_response = capabilities
        self.projects_response = projects
        self.calls: list[tuple[str, str | None]] = []
        self.mutation_calls = 0

    def capabilities(self) -> OperatorResponse:
        self.calls.append(("capabilities", None))
        return self.capabilities_response

    def list_projects(self, workspaces_root: str | Path) -> OperatorResponse:
        self.calls.append(("list_projects", str(workspaces_root)))
        if self.projects_response is None:
            raise AssertionError("list_projects was not expected")
        return self.projects_response

    def prepare(self, *args, **kwargs):  # pragma: no cover - safety guard
        self.mutation_calls += 1
        raise AssertionError("Bootstrap must not call prepare")

    def start(self, *args, **kwargs):  # pragma: no cover - safety guard
        self.mutation_calls += 1
        raise AssertionError("Bootstrap must not call start")

    def stop(self, *args, **kwargs):  # pragma: no cover - safety guard
        self.mutation_calls += 1
        raise AssertionError("Bootstrap must not call stop")

    def rearm(self, *args, **kwargs):  # pragma: no cover - safety guard
        self.mutation_calls += 1
        raise AssertionError("Bootstrap must not call rearm")


def capabilities_response(*, network_listener: bool = False, arbitrary_shell: bool = False) -> OperatorResponse:
    return OperatorResponse.success(
        "capabilities",
        data={
            "transport": "in_process",
            "network_listener": network_listener,
            "arbitrary_shell": arbitrary_shell,
            "journal_access": "read_only",
        },
    )


def projects_response(projects: list[dict[str, object]]) -> OperatorResponse:
    return OperatorResponse.success(
        "list_projects",
        data={
            "projects": projects,
            "invalid_entries": [{"path": "C:/broken", "code": "workspace_state_invalid"}],
        },
    )


def project(alias: str) -> dict[str, object]:
    return {
        "schema": "bdb-operator-project-v1",
        "alias": alias,
        "workspace_root": f"C:/workspaces/{alias}",
        "source_repo": f"C:/source/{alias}",
        "source_branch": "main",
        "configured_status": "prepared",
        "allowed_paths": ["README.md", "tests/*.py"],
    }


def test_bootstrap_uses_exactly_two_read_only_operations(tmp_path: Path) -> None:
    operator = FakeBootstrapOperator(
        capabilities=capabilities_response(),
        projects=projects_response([project("zeta"), project("alpha")]),
    )

    snapshot = BootstrapService(operator).load(tmp_path)

    assert snapshot.schema == GUI_BOOTSTRAP_SCHEMA
    assert snapshot.ok is True
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0
    assert [item.alias for item in snapshot.projects] == ["alpha", "zeta"]
    assert snapshot.operator_transport == "in_process"
    assert snapshot.network_listener is False
    assert snapshot.journal_access == "read_only"
    assert snapshot.invalid_entries == (
        {"path": "C:/broken", "code": "workspace_state_invalid"},
    )
    assert operator.calls == [
        ("capabilities", None),
        ("list_projects", str(tmp_path.resolve())),
    ]
    assert operator.mutation_calls == 0


def test_unsafe_capabilities_stop_before_project_discovery(tmp_path: Path) -> None:
    operator = FakeBootstrapOperator(
        capabilities=capabilities_response(network_listener=True),
    )

    snapshot = BootstrapService(operator).load(tmp_path)

    assert snapshot.ok is False
    assert snapshot.error_code == "unsafe_operator_capabilities"
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0
    assert operator.calls == [("capabilities", None)]


def test_arbitrary_shell_capability_is_rejected(tmp_path: Path) -> None:
    operator = FakeBootstrapOperator(
        capabilities=capabilities_response(arbitrary_shell=True),
    )

    snapshot = BootstrapService(operator).load(tmp_path)

    assert snapshot.ok is False
    assert snapshot.error_code == "unsafe_operator_capabilities"
    assert operator.calls == [("capabilities", None)]


def test_capabilities_error_is_preserved_without_project_read(tmp_path: Path) -> None:
    operator = FakeBootstrapOperator(
        capabilities=OperatorResponse.failure(
            "capabilities",
            error=OperatorError(
                code="operator_unavailable",
                message="Operator API unavailable",
            ),
        )
    )

    snapshot = BootstrapService(operator).load(tmp_path)

    assert snapshot.ok is False
    assert snapshot.error_code == "operator_unavailable"
    assert snapshot.error_message == "Operator API unavailable"
    assert snapshot.operator_schema == "bdb-operator-response-v1"
    assert operator.calls == [("capabilities", None)]


def test_project_discovery_error_becomes_failure_snapshot(tmp_path: Path) -> None:
    operator = FakeBootstrapOperator(
        capabilities=capabilities_response(),
        projects=OperatorResponse.failure(
            "list_projects",
            error=OperatorError(
                code="invalid_argument",
                message="Workspaces root does not exist",
            ),
        ),
    )

    snapshot = BootstrapService(operator).load(tmp_path / "missing")

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_argument"
    assert snapshot.error_message == "Workspaces root does not exist"
    assert snapshot.mutation_operations_invoked == 0
    assert [call[0] for call in operator.calls] == ["capabilities", "list_projects"]


def test_invalid_project_document_is_rejected_without_mutation(tmp_path: Path) -> None:
    invalid = project("alpha")
    invalid.pop("source_repo")
    operator = FakeBootstrapOperator(
        capabilities=capabilities_response(),
        projects=projects_response([invalid]),
    )

    snapshot = BootstrapService(operator).load(tmp_path)

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
    assert "source_repo" in (snapshot.error_message or "")
    assert operator.mutation_calls == 0


def test_snapshot_serialization_remains_explicitly_read_only(tmp_path: Path) -> None:
    operator = FakeBootstrapOperator(
        capabilities=capabilities_response(),
        projects=projects_response([]),
    )

    document = BootstrapService(operator).load(tmp_path).to_dict()

    assert document["schema"] == GUI_BOOTSTRAP_SCHEMA
    assert document["ok"] is True
    assert document["read_only"] is True
    assert document["mutation_operations_invoked"] == 0
    assert document["projects"] == []
    assert document["error"] is None
