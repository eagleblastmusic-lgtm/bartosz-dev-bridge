from __future__ import annotations

import copy
from pathlib import Path

from bdb_gui.session_history import SessionHistoryService, SessionHistorySnapshot
from bdb_operator import OperatorApi, OperatorResponse

from session_projection_fixture import (
    CORRELATION_ID,
    FAILED_SESSION,
    SUCCESS_SESSION,
    workspace_fixture,
)


def test_gui_session_history_parses_explicit_verified_repair_group(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    service = SessionHistoryService(OperatorApi(repo_root=tmp_path, platform_name="posix"))

    snapshot = service.read(root, limit=10)

    assert snapshot.ok is True
    assert snapshot.read_only is True
    assert snapshot.repair_relationships_inferred is False
    assert [session.session_id for session in snapshot.sessions] == [SUCCESS_SESSION, FAILED_SESSION]
    assert snapshot.sessions[0].latest_attempt is not None
    assert snapshot.sessions[0].latest_attempt.promotion_status == "promoted"
    assert snapshot.sessions[0].repair_correlation is not None
    assert snapshot.sessions[0].repair_correlation.role == "repair"
    assert snapshot.sessions[0].repair_correlation.predecessor_session_id == FAILED_SESSION
    assert snapshot.sessions[1].latest_attempt is not None
    assert snapshot.sessions[1].latest_attempt.rollback_performed is True
    assert snapshot.sessions[1].repair_correlation is not None
    assert snapshot.sessions[1].repair_correlation.role == "initial"
    assert len(snapshot.repair_groups) == 1
    group = snapshot.repair_groups[0]
    assert group.correlation_id == CORRELATION_ID
    assert group.verified is True
    assert group.initial_session_id == FAILED_SESSION
    assert group.repair_session_ids == (SUCCESS_SESSION,)
    assert group.edges == ((FAILED_SESSION, SUCCESS_SESSION),)
    assert group.relationship_inferred is False
    assert snapshot.group_for_session(SUCCESS_SESSION) == group
    assert snapshot.group_for_session(FAILED_SESSION) == group
    assert snapshot.mutation_operations_invoked == 0


def test_gui_rejects_operator_response_that_infers_repair_relationship(tmp_path: Path) -> None:
    response = OperatorResponse.success(
        "sessions",
        operation_id="session-history-invalid",
        project_alias="sample",
        data={
            "schema": "bdb-session-history-v1",
            "project_alias": "sample",
            "generated_at": "2026-07-19T19:00:00Z",
            "limit": 20,
            "read_only": True,
            "repair_relationships_inferred": True,
            "sessions": [],
            "repair_groups": [],
        },
    )

    snapshot = SessionHistorySnapshot.from_response(tmp_path, response, requested_limit=20)

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
    assert "safety flags" in (snapshot.error_message or "")


def test_gui_rejects_repair_group_edge_outside_bounded_response(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    api = OperatorApi(repo_root=tmp_path, platform_name="posix")
    valid = api.sessions(root, limit=10)
    assert valid.ok is True
    data = copy.deepcopy(valid.data)
    data["repair_groups"][0]["edges"][0]["repair_session_id"] = "outside-session"
    response = OperatorResponse.success(
        "sessions",
        operation_id="session-history-invalid-edge",
        project_alias="sample",
        data=data,
    )

    snapshot = SessionHistorySnapshot.from_response(root, response, requested_limit=10)

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
    assert "non-member" in (snapshot.error_message or "")


def test_gui_rejects_session_group_id_without_matching_explicit_correlation(tmp_path: Path) -> None:
    root, _, _, _ = workspace_fixture(tmp_path)
    api = OperatorApi(repo_root=tmp_path, platform_name="posix")
    valid = api.sessions(root, limit=10)
    assert valid.ok is True
    data = copy.deepcopy(valid.data)
    data["sessions"][0]["repair_group_id"] = "different-correlation"
    response = OperatorResponse.success(
        "sessions",
        operation_id="session-history-invalid-group",
        project_alias="sample",
        data=data,
    )

    snapshot = SessionHistorySnapshot.from_response(root, response, requested_limit=10)

    assert snapshot.ok is False
    assert snapshot.error_code == "invalid_operator_response"
    assert "does not match explicit correlation" in (snapshot.error_message or "")


def test_gui_session_history_validates_limit_before_operator_call(tmp_path: Path) -> None:
    class UnusedOperator:
        def sessions(self, workspace_root, *, limit):  # pragma: no cover - failure guard
            raise AssertionError("invalid limit must be rejected before Operator API")

    service = SessionHistoryService(UnusedOperator())  # type: ignore[arg-type]

    try:
        service.read(tmp_path, limit=101)
    except ValueError as error:
        assert "between 1 and 100" in str(error)
    else:  # pragma: no cover
        raise AssertionError("invalid limit was accepted")
