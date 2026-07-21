from __future__ import annotations

from bdb_operator.session_projection import _result_summary


def test_session_projection_exposes_exact_terminal_detail() -> None:
    row = {
        "result_status": "policy_denied",
        "error_code": "policy_denied",
        "operation": "multi_file_patch",
        "result_sha256": "sha256:" + "a" * 64,
    }
    document = {
        "status": "policy_denied",
        "error_code": "unsafe_path",
        "summary": "Command ended before file mutation",
        "exit_code": None,
        "changed_files": [],
        "data": {
            "operation": "multi_file_patch",
            "terminal_error_code": "unsafe_path",
            "terminal_detail": "Path is not allowed by local policy: START-MP4-PLAYER.cmd",
        },
    }

    summary = _result_summary(document, row)

    assert summary["status"] == "policy_denied"
    assert summary["error_code"] == (
        "unsafe_path — Path is not allowed by local policy: START-MP4-PLAYER.cmd"
    )
    assert summary["terminal_detail"] == (
        "Path is not allowed by local policy: START-MP4-PLAYER.cmd"
    )
    assert summary["summary"] == "Command ended before file mutation"
