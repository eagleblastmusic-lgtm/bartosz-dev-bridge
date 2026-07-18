from __future__ import annotations

from pathlib import Path

from bdb_gui.diagnostics import (
    MAX_DIAGNOSTIC_LOG_BYTES,
    MAX_DIAGNOSTIC_LOG_LINES,
    REDACTION_VERSION,
    DiagnosticsService,
)
from bdb_operator.models import OperatorError, OperatorResponse


class FakeDiagnosticsOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.capabilities_response = OperatorResponse.success(
            "capabilities",
            operation_id="cap-op",
            data={
                "transport": "in_process",
                "api_token": "super-secret-token",
                "nested": {"password": "hunter2"},
            },
        )
        self.status_response = OperatorResponse.success(
            "status",
            project_alias="alpha",
            operation_id="status-op",
            data={
                "status": "RUNNING",
                "note": "Authorization: Bearer abc.def.ghi",
            },
        )
        self.current_response = OperatorResponse.success(
            "current_operation",
            project_alias="alpha",
            operation_id="current-op",
            data={"schema": "bdb-current-operation-v1", "active": False, "operation": None},
        )
        self.logs_response = OperatorResponse.success(
            "logs",
            project_alias="alpha",
            operation_id="logs-op",
            data={
                "schema": "bdb-log-snapshot-v1",
                "limits": {
                    "max_bytes_per_source": MAX_DIAGNOSTIC_LOG_BYTES,
                    "max_lines_per_source": MAX_DIAGNOSTIC_LOG_LINES,
                },
                "sources": [
                    {
                        "source": "promoter_stdout",
                        "path": "C:/logs/promoter.out.log",
                        "exists": True,
                        "size_bytes": 18,
                        "modified_at": "2026-07-18T21:00:00Z",
                        "truncated": False,
                        "lines": ["token=abc123", "normal line"],
                    }
                ],
            },
        )

    def capabilities(self) -> OperatorResponse:
        self.calls.append(("capabilities",))
        return self.capabilities_response

    def status(self, workspace_root: str | Path) -> OperatorResponse:
        self.calls.append(("status", str(workspace_root)))
        return self.status_response

    def current_operation(self, workspace_root: str | Path) -> OperatorResponse:
        self.calls.append(("current_operation", str(workspace_root)))
        return self.current_response

    def logs(
        self,
        workspace_root: str | Path,
        *,
        max_bytes: int = MAX_DIAGNOSTIC_LOG_BYTES,
        max_lines: int = MAX_DIAGNOSTIC_LOG_LINES,
    ) -> OperatorResponse:
        self.calls.append(("logs", str(workspace_root), max_bytes, max_lines))
        return self.logs_response


def test_collect_uses_exact_four_bounded_read_operations(tmp_path: Path) -> None:
    operator = FakeDiagnosticsOperator()
    workspace = tmp_path / "alpha"

    snapshot = DiagnosticsService(operator).collect(workspace)

    assert snapshot.complete is True
    assert snapshot.read_only is True
    assert snapshot.mutation_operations_invoked == 0
    assert snapshot.redaction_version == REDACTION_VERSION
    assert snapshot.project_alias == "alpha"
    assert [section.name for section in snapshot.sections] == [
        "capabilities",
        "status",
        "current_operation",
        "logs",
    ]
    assert operator.calls == [
        ("capabilities",),
        ("status", str(workspace.resolve())),
        ("current_operation", str(workspace.resolve())),
        (
            "logs",
            str(workspace.resolve()),
            MAX_DIAGNOSTIC_LOG_BYTES,
            MAX_DIAGNOSTIC_LOG_LINES,
        ),
    ]
    assert snapshot.versions["python"]
    assert snapshot.versions["bartosz_dev_bridge"]


def test_collect_redacts_secret_keys_assignments_and_bearer_values(tmp_path: Path) -> None:
    snapshot = DiagnosticsService(FakeDiagnosticsOperator()).collect(tmp_path / "alpha")
    rendered = snapshot.to_dict()

    capabilities = rendered["sections"][0]["data"]
    status = rendered["sections"][1]["data"]
    logs = rendered["sections"][3]["data"]

    assert capabilities["api_token"] == "[REDACTED]"
    assert capabilities["nested"]["password"] == "[REDACTED]"
    assert "abc.def.ghi" not in status["note"]
    assert "[REDACTED]" in status["note"]
    assert logs["sources"][0]["lines"][0] == "token=[REDACTED]"
    assert "super-secret-token" not in str(rendered)
    assert "hunter2" not in str(rendered)
    assert "abc123" not in str(rendered)


def test_one_operator_failure_produces_partial_snapshot_without_aborting(tmp_path: Path) -> None:
    operator = FakeDiagnosticsOperator()
    operator.current_response = OperatorResponse.failure(
        "current_operation",
        project_alias="alpha",
        operation_id="current-failed",
        error=OperatorError(code="journal_missing", message="Journal missing"),
    )

    snapshot = DiagnosticsService(operator).collect(tmp_path / "alpha")

    assert snapshot.complete is False
    assert len(snapshot.sections) == 4
    failed = snapshot.sections[2]
    assert failed.name == "current_operation"
    assert failed.ok is False
    assert failed.error_code == "journal_missing"
    assert failed.error_message == "Journal missing"
    assert snapshot.sections[3].ok is True
    assert snapshot.mutation_operations_invoked == 0


def test_snapshot_document_contains_only_sanitized_section_data(tmp_path: Path) -> None:
    snapshot = DiagnosticsService(FakeDiagnosticsOperator()).collect(tmp_path / "alpha")
    document = snapshot.to_dict()

    assert document["schema"] == "bdb-gui-diagnostics-v1"
    assert document["read_only"] is True
    assert document["mutation_operations_invoked"] == 0
    assert document["redaction_version"] == "bdb-redaction-v1"
    assert len(document["sections"]) == 4
    assert all(
        section["schema"] == "bdb-gui-diagnostics-section-v1"
        for section in document["sections"]
    )
