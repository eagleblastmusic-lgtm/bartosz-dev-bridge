from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bdb_vnext.project_execution import ProjectExecutionError, ProjectExecutionSubmission, _final_result_status


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension_vnext"
HARNESS = ROOT / "tests" / "fixtures" / "vnext_project_result_browser_runtime.cjs"


def _browser(mode: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for Browser/Native runtime regression")
    completed = subprocess.run(
        [node, str(HARNESS), str(EXTENSION / "transport_worker.js"), str(EXTENSION / "content_adapter.js"), mode],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("mode", ["numeric", "text"])
def test_plan_version_browser_result_roundtrips_through_strict_native_parser(mode: str) -> None:
    observed = _browser(mode)
    assert observed["first"]["parsed"]["plan_version"] == "1"
    assert observed["beforeFinal"] == 1
    assert observed["submissions"][0]["result"]["plan_version"] == "1"
    assert ProjectExecutionSubmission.from_mapping(observed["submissions"][0]["result"]).plan_version == "1"


def test_native_error_code_and_message_reach_browser_result_panel() -> None:
    observed = _browser("native-error")
    assert observed["beforeFinal"] == 1
    assert observed["first"]["state"] == "error"
    assert "execution_field_invalid" in observed["first"]["text"]
    assert "plan_version must be text" in observed["first"]["text"]
    assert "project execution result rejected" not in observed["first"]["text"]


def test_waiting_external_never_calls_final_native_endpoint_and_later_pass_uses_same_binding() -> None:
    observed = _browser("waiting")
    assert observed["beforeFinal"] == 0
    assert observed["first"]["state"] == "warning"
    assert "execution_result_non_terminal" in observed["first"]["text"]
    assert len(observed["browserMessages"]) == 2
    assert len(observed["submissions"]) == 1
    assert observed["second"]["state"] == "success"
    assert observed["submissions"][0]["result"]["execution_binding_id"] == observed["first"]["parsed"]["execution_binding_id"]


def test_browser_pre_submit_gate_rejects_known_invalid_head_before_native() -> None:
    observed = _browser("invalid")
    assert observed["beforeFinal"] == 0
    assert observed["first"]["state"] == "error"
    assert "execution_field_invalid" in observed["first"]["text"]


def test_worker_rejects_direct_numeric_plan_version_before_native() -> None:
    observed = _browser("direct-numeric")
    assert observed["response"]["ok"] is False
    assert observed["response"]["error_code"] == "execution_field_invalid"
    assert observed["submissions"] == []


def test_browser_and_native_final_status_models_agree() -> None:
    model = _browser("status-model")
    for field, cases in model.items():
        for status, browser_classification in cases.items():
            try:
                assert _final_result_status(status, field) == status
                assert browser_classification in {"TERMINAL_SUCCESS", "TERMINAL_FAILURE"}
            except ProjectExecutionError as error:
                assert browser_classification == error.code
    assert model["execution_status"]["WAITING_EXTERNAL"] == "execution_result_non_terminal"
    assert model["validation_status"]["AWAITING_CI"] == "execution_result_non_terminal"
    assert model["promotion_status"]["NOT_RUN"] == "TERMINAL_FAILURE"
