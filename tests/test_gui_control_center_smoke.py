from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_smoke(workspaces_root: Path, report_path: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bdb_gui.app",
            "--workspaces-root",
            str(workspaces_root),
            "--headless-smoke",
            "--smoke-timeout-ms",
            "15000",
            "--json-out",
            str(report_path),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_control_center_empty_root_headless_smoke(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    workspaces_root = tmp_path / "workspaces"
    workspaces_root.mkdir()
    report_path = tmp_path / "control-center-smoke.json"

    completed = run_smoke(workspaces_root, report_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == "bdb-control-center-smoke-v1"
    assert report["status"] == "success"
    assert report["application_version"] == "0.3.0"
    assert report["window_object_name"] == "BdbControlCenterWindow"
    assert report["window_constructed"] is True
    assert report["read_only_startup"] is True
    assert report["bootstrap_completed"] is True
    assert report["bootstrap_ok"] is True
    assert report["bootstrap_error_code"] is None
    assert report["project_count"] == 0
    assert report["page_count"] == 5
    assert report["navigation"] == [
        "Dashboard",
        "Projects",
        "Current operation",
        "History",
        "Diagnostics",
    ]
    assert report["operation_flow_present"] is True
    assert report["current_operation_read_only"] is True
    assert report["history_tabs_present"] is True
    assert report["session_history_view_present"] is True
    assert report["session_history_read_only"] is True
    assert report["session_result_open_explicit"] is True
    assert report["session_receipt_open_explicit"] is True
    assert report["session_folder_open_explicit"] is True
    assert report["session_repair_relationships_inferred"] is False
    assert report["mutation_operations_invoked"] == 0
    assert report["operator_network_listener"] is False
    assert report["qt_platform"] == "offscreen"
    assert report["event_loop_exit_code"] == 0
    assert report["timed_out"] is False


def test_control_center_discovers_prepared_project_without_runtime_calls(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    workspaces_root = tmp_path / "workspaces"
    project_root = workspaces_root / "alpha"
    project_root.mkdir(parents=True)
    state = {
        "schema": "bdb-workspace-loop-state-v1",
        "status": "prepared",
        "alias": "alpha",
        "source_repo": str(tmp_path / "source-alpha"),
        "source_branch": "main",
        "source_head": "a" * 40,
        "root": str(project_root),
        "bridge_config": str(project_root / "bridge-config.json"),
        "native_config": str(project_root / "native-config.json"),
        "python_executable": str(tmp_path / "python.exe"),
        "promoter_script": str(tmp_path / "promoter.py"),
        "promoter_pid_file": str(project_root / "promoter.pid"),
        "promoter_stop_file": str(project_root / "promoter.stop"),
        "promoter_stdout": str(project_root / "promoter.out.log"),
        "promoter_stderr": str(project_root / "promoter.err.log"),
        "allowed_paths": ["README.md", "tests/*.py"],
    }
    (project_root / "workspace-loop-state.json").write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    report_path = tmp_path / "prepared-project-smoke.json"

    completed = run_smoke(workspaces_root, report_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "success"
    assert report["application_version"] == "0.3.0"
    assert report["project_count"] == 1
    assert report["mutation_operations_invoked"] == 0
    assert report["operator_network_listener"] is False


def test_control_center_missing_root_fails_without_creating_it(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    workspaces_root = tmp_path / "missing-workspaces"
    report_path = tmp_path / "missing-root-smoke.json"

    completed = run_smoke(workspaces_root, report_path)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["application_version"] == "0.3.0"
    assert report["bootstrap_completed"] is True
    assert report["bootstrap_ok"] is False
    assert report["bootstrap_error_code"] == "invalid_argument"
    assert report["mutation_operations_invoked"] == 0
    assert report["operator_network_listener"] is None
    assert not workspaces_root.exists()
