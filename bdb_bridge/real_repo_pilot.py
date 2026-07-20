from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .one_message_pilot_support import (
    assert_clean_checkout,
    canonical_time,
    git,
    initialize_control,
    load_json_output,
    run,
    service_json,
    submit_patch,
    wait_until,
)
from .real_repo_pilot_fixture import (
    ALIAS,
    EXPECTED_FAILED_TEST,
    PINNED_SHA,
    REMOTE_URL,
    build_configs,
    failed_operations,
    initialize_kalkulator,
    remote_main_sha,
    repaired_operations,
)
from .repair_loop import analyze_failed_pytest_result
from .workspace_promoter import WorkspacePromoter


REPORT_SCHEMA = "bdb-real-repository-pilot-report-v1"


def _assert_original_bytes(workspace: Path, data: dict[str, Any], *, label: str) -> None:
    expected = {
        "calculator.py": data["calculator_before"],
        "tests/test_calculator.py": data["tests_before"],
        "README.md": data["readme_before"],
    }
    for relative, content in expected.items():
        if (workspace / relative).read_bytes() != content:
            raise RuntimeError(f"{label} did not preserve original bytes: {relative}")


def _assert_repaired_bytes(workspace: Path, data: dict[str, Any]) -> None:
    expected = {
        "calculator.py": data["calculator_repaired"],
        "tests/test_calculator.py": data["tests_after"],
        "README.md": data["readme_after"],
    }
    for relative, content in expected.items():
        if (workspace / relative).read_bytes() != content:
            raise RuntimeError(f"Promoted real-repository file differs: {relative}")


def execute_pilot(*, repo_root: Path, root: Path, python_executable: str, timeout: float) -> dict[str, Any]:
    correlation_id = str(uuid.uuid4())
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "failed",
        "task": "Add CalculatorEngine.square and keyboard shortcut s to the pinned kalkulator repository",
        "started_at": canonical_time(datetime.now(timezone.utc)),
        "repository": {"url": REMOTE_URL, "pinned_sha": PINNED_SHA, "remote_mutation_allowed": False},
        "allowed_paths": ["calculator.py", "tests/test_calculator.py", "README.md"],
        "profile_id": "poc_pytest",
        "attempt_limit": 2,
        "user_interventions_between_attempts": 0,
        "repair_correlation": {"schema": "bdb-repair-correlation-v1", "correlation_id": correlation_id},
    }
    report_path = root / "real-repository-pilot-report.json"
    stdout_path = root / "bridge.stdout.log"
    stderr_path = root / "bridge.stderr.log"
    service: subprocess.Popen[Any] | None = None
    stdout_handle = None
    stderr_handle = None

    try:
        data = initialize_kalkulator(root)
        fixture = Path(data["fixture"])
        _, control = initialize_control(root)
        bridge_config_path, native_config_path = build_configs(root, fixture, control, python_executable)
        config = BridgeConfig.from_json(bridge_config_path)

        baseline = run([python_executable, "-m", "pytest", "-q"], cwd=fixture)
        report["baseline_tests"] = {"exit_code": baseline.returncode, "stdout_tail": str(baseline.stdout)[-2000:]}
        report["remote_main_before"] = data["remote_main_before"]
        report["local_remote_count_after_detach"] = len(git(fixture, "remote").splitlines())

        arm = run(
            [python_executable, "-m", "bdb_bridge", "bridge", "native-host", "arm", "--config", str(native_config_path), "--minutes", "10"],
            cwd=repo_root,
        )
        report["arm"] = load_json_output(arm)
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        service = subprocess.Popen(
            [python_executable, "-m", "bdb_bridge", "bridge", "start", "--config", str(bridge_config_path), "--foreground"],
            cwd=repo_root,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        report["running"] = wait_until(
            "Bridge RUNNING",
            lambda: service_json(python_executable, repo_root, bridge_config_path, "status"),
            lambda value: value.get("status") == "RUNNING",
            timeout=timeout,
            process=service,
        )

        first_response = submit_patch(
            repo_root=repo_root,
            python_executable=python_executable,
            native_config=native_config_path,
            request_id="kalkulator-real-initial-attempt",
            repo_alias=ALIAS,
            profile_id="poc_pytest",
            repair_correlation={
                "schema": "bdb-repair-correlation-v1",
                "correlation_id": correlation_id,
                "role": "initial",
                "predecessor_session_id": None,
            },
            operations=failed_operations(data),
            timeout=timeout,
        )
        first_result = first_response["result"]
        first_data = first_result.get("data")
        analysis = analyze_failed_pytest_result(first_result, expected_test=EXPECTED_FAILED_TEST)
        if first_result.get("status") != "failed" or first_result.get("exit_code") == 0:
            raise RuntimeError("Initial real-repository candidate did not fail as required")
        if not isinstance(first_data, dict) or first_data.get("checkpoint_state") != "rolled_back":
            raise RuntimeError("Initial real-repository candidate did not record rollback")
        if first_data.get("rollback_performed") is not True or first_result.get("changed_files") != []:
            raise RuntimeError("Initial real-repository rollback evidence is invalid")

        first_command_id = str(first_response["command_id"])
        first_session_id = first_command_id.split(":", 1)[0]
        failed_workspace = Path(config.worktree_root) / first_session_id
        _assert_original_bytes(failed_workspace, data, label="Failed isolated workspace")
        assert_clean_checkout(failed_workspace, label="failed real-repository workspace")
        _assert_original_bytes(fixture, data, label="Source checkout after failure")
        assert_clean_checkout(fixture, label="source checkout after failed real-repository attempt")

        second_response = submit_patch(
            repo_root=repo_root,
            python_executable=python_executable,
            native_config=native_config_path,
            request_id="kalkulator-real-repair-attempt",
            repo_alias=ALIAS,
            profile_id="poc_pytest",
            repair_correlation={
                "schema": "bdb-repair-correlation-v1",
                "correlation_id": correlation_id,
                "role": "repair",
                "predecessor_session_id": first_session_id,
            },
            operations=repaired_operations(data),
            timeout=timeout,
        )
        second_result = second_response["result"]
        second_data = second_result.get("data")
        if second_result.get("status") != "success" or second_result.get("exit_code") != 0:
            raise RuntimeError(f"Real-repository repair did not pass: {second_result}")
        if not isinstance(second_data, dict) or second_data.get("checkpoint_state") != "committed":
            raise RuntimeError("Real-repository repair did not commit its checkpoint")
        if second_data.get("rollback_performed") is not False:
            raise RuntimeError("Successful real-repository repair unexpectedly rolled back")

        second_command_id = str(second_response["command_id"])
        second_session_id = second_command_id.split(":", 1)[0]
        if second_session_id == first_session_id:
            raise RuntimeError("Real-repository repair must use a distinct session")
        result_path = Path(config.direct_result_dir) / "sessions" / second_session_id / "results" / "000001.json"
        if not result_path.is_file():
            raise RuntimeError("Durable real-repository success result is missing")

        promotion = WorkspacePromoter(config).promote_file(result_path)
        if promotion.status != "promoted" or promotion.source_commit is None:
            raise RuntimeError(f"Real-repository result was not promoted: {promotion.as_dict()}")
        if promotion.parent_commit != PINNED_SHA:
            raise RuntimeError("Real-repository promotion parent differs from pinned commit")

        final_tests = run([python_executable, "-m", "pytest", "-q"], cwd=fixture)
        assert_clean_checkout(fixture, label="promoted real-repository checkout")
        if git(fixture, "rev-parse", "HEAD") != promotion.source_commit:
            raise RuntimeError("Local source HEAD differs from promotion receipt")
        if git(fixture, "branch", "--show-current") != "bdb-real-pilot":
            raise RuntimeError("Real-repository promotion left the local-only branch")
        if git(fixture, "remote"):
            raise RuntimeError("Real-repository promotion restored a Git remote")
        _assert_repaired_bytes(fixture, data)

        remote_after = remote_main_sha()
        if remote_after != data["remote_main_before"] or remote_after != PINNED_SHA:
            raise RuntimeError("Remote kalkulator main changed during the local pilot")

        stop = run(
            [python_executable, "-m", "bdb_bridge", "bridge", "stop", "--config", str(bridge_config_path)],
            cwd=repo_root,
        )
        report["stop"] = load_json_output(stop)
        service.wait(timeout=timeout)
        if service.returncode != 0:
            raise RuntimeError(f"Bridge exited with code {service.returncode}")
        service = None

        report.update(
            {
                "status": "pass",
                "completed_at": canonical_time(datetime.now(timezone.utc)),
                "attempt_count": 2,
                "initial_attempt": {
                    "command_id": first_command_id,
                    "session_id": first_session_id,
                    "status": first_result.get("status"),
                    "exit_code": first_result.get("exit_code"),
                    "checkpoint_state": first_data.get("checkpoint_state"),
                    "rollback_performed": first_data.get("rollback_performed"),
                    "analysis": analysis.as_dict(),
                    "workspace_clean_after_rollback": True,
                    "repair_role": "initial",
                },
                "repair_attempt": {
                    "command_id": second_command_id,
                    "session_id": second_session_id,
                    "status": second_result.get("status"),
                    "exit_code": second_result.get("exit_code"),
                    "checkpoint_state": second_data.get("checkpoint_state"),
                    "changed_files": second_result.get("changed_files"),
                    "repair_role": "repair",
                    "predecessor_session_id": first_session_id,
                },
                "repair_correlation": {
                    "schema": "bdb-repair-correlation-v1",
                    "correlation_id": correlation_id,
                    "initial_session_id": first_session_id,
                    "repair_session_id": second_session_id,
                    "verified": True,
                },
                "promotion": promotion.as_dict(),
                "final_tests": {"exit_code": final_tests.returncode, "stdout_tail": str(final_tests.stdout)[-2000:]},
                "source_checkout_clean": True,
                "local_branch": "bdb-real-pilot",
                "local_source_commit": promotion.source_commit,
                "local_remote_count": 0,
                "remote_main_after": remote_after,
                "remote_mutation_performed": False,
                "receipt_path": str(promotion.receipt_path),
                "journal_path": str(config.journal_path),
                "artifacts": {
                    "bridge_config": str(bridge_config_path),
                    "native_config": str(native_config_path),
                    "bridge_stdout": str(stdout_path),
                    "bridge_stderr": str(stderr_path),
                },
            }
        )
        return report
    finally:
        if service is not None and service.poll() is None:
            service.terminate()
            try:
                service.wait(timeout=10)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=10)
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded failure-repair pilot on a pinned real GitHub repository clone.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    root = Path(args.root).expanduser().resolve(strict=False)
    python_executable = str(Path(args.python).expanduser().resolve(strict=True))
    if root.exists():
        raise RuntimeError(f"Pilot root already exists: {root}")
    try:
        root.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise RuntimeError("Pilot root must stay outside the implementation checkout")
    root.mkdir(parents=True)

    report: dict[str, Any] = {"schema": REPORT_SCHEMA, "status": "failed", "started_at": canonical_time(datetime.now(timezone.utc))}
    try:
        report = execute_pilot(repo_root=repo_root, root=root, python_executable=python_executable, timeout=args.timeout)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["completed_at"] = canonical_time(datetime.now(timezone.utc))
        (root / "real-repository-pilot-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
