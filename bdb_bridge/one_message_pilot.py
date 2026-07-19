from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .one_message_pilot_fixture import (
    EXPECTED_FAILED_TEST,
    build_configs,
    create_file,
    initialize_calculator2,
    replacement,
)
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
from .repair_loop import analyze_failed_pytest_result
from .workspace_promoter import WorkspacePromoter


def execute_pilot(*, repo_root: Path, root: Path, python_executable: str, timeout: float) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "bdb-one-message-repair-pilot-report-v1",
        "status": "failed",
        "task": "Add safe_divide to calculator2 and return None for division by zero",
        "started_at": canonical_time(datetime.now(timezone.utc)),
        "user_interventions_between_attempts": 0,
    }
    report_path = root / "one-message-repair-report.json"
    stdout_path = root / "bridge.stdout.log"
    stderr_path = root / "bridge.stderr.log"
    service: subprocess.Popen[Any] | None = None
    stdout_handle = None
    stderr_handle = None

    try:
        fixture_data = initialize_calculator2(root)
        fixture = Path(fixture_data["fixture"])
        _, control = initialize_control(root)
        bridge_config_path, native_config_path = build_configs(root, fixture, control, python_executable)
        config = BridgeConfig.from_json(bridge_config_path)

        baseline = run([python_executable, "-m", "pytest", "-q"], cwd=fixture)
        report["baseline_tests"] = {"exit_code": baseline.returncode, "stdout_tail": str(baseline.stdout)[-2000:]}

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
            request_id="calculator2-initial-attempt",
            operations=[
                replacement("src/calculator.py", fixture_data["source_before"], fixture_data["source_failed"]),
                replacement("tests/test_calculator.py", fixture_data["tests_before"], fixture_data["tests_after"]),
            ],
            timeout=timeout,
        )
        first_result = first_response["result"]
        analysis = analyze_failed_pytest_result(first_result, expected_test=EXPECTED_FAILED_TEST)
        first_command_id = str(first_response["command_id"])
        first_session_id = first_command_id.split(":", 1)[0]
        failed_workspace = Path(config.worktree_root) / first_session_id
        if (failed_workspace / "src" / "calculator.py").read_bytes() != fixture_data["source_before"]:
            raise RuntimeError("Failed workspace did not roll back calculator.py")
        if (failed_workspace / "tests" / "test_calculator.py").read_bytes() != fixture_data["tests_before"]:
            raise RuntimeError("Failed workspace did not roll back test_calculator.py")
        assert_clean_checkout(failed_workspace, label="failed isolated workspace")
        assert_clean_checkout(fixture, label="source checkout after failed attempt")

        pilot_note = (
            "# One-message repair pilot\n\n"
            "The first candidate failed pytest, was rolled back, and the repaired candidate passed.\n"
        ).encode("utf-8")
        second_response = submit_patch(
            repo_root=repo_root,
            python_executable=python_executable,
            native_config=native_config_path,
            request_id="calculator2-repair-attempt",
            operations=[
                replacement("src/calculator.py", fixture_data["source_before"], fixture_data["source_repaired"]),
                replacement("tests/test_calculator.py", fixture_data["tests_before"], fixture_data["tests_after"]),
                create_file("PILOT_RESULT.md", pilot_note),
            ],
            timeout=timeout,
        )
        second_result = second_response["result"]
        second_data = second_result.get("data")
        if second_result.get("status") != "success" or second_result.get("exit_code") != 0:
            raise RuntimeError(f"Repair attempt did not pass: {second_result}")
        if not isinstance(second_data, dict) or second_data.get("checkpoint_state") != "committed":
            raise RuntimeError("Repair attempt did not commit its checkpoint")
        if second_data.get("rollback_performed") is not False:
            raise RuntimeError("Repair attempt unexpectedly reported rollback")

        second_command_id = str(second_response["command_id"])
        second_session_id = second_command_id.split(":", 1)[0]
        result_path = Path(config.direct_result_dir) / "sessions" / second_session_id / "results" / "000001.json"
        if not result_path.is_file():
            raise RuntimeError("Durable successful result is missing")
        promotion = WorkspacePromoter(config).promote_file(result_path)
        if promotion.status != "promoted" or promotion.source_commit is None:
            raise RuntimeError(f"Repair result was not promoted: {promotion.as_dict()}")

        final_tests = run([python_executable, "-m", "pytest", "-q"], cwd=fixture)
        assert_clean_checkout(fixture, label="promoted source checkout")
        if git(fixture, "rev-parse", "HEAD") != promotion.source_commit:
            raise RuntimeError("Source HEAD does not match the promotion receipt")
        if (fixture / "src" / "calculator.py").read_bytes() != fixture_data["source_repaired"]:
            raise RuntimeError("Promoted calculator.py differs from the repaired candidate")
        if (fixture / "tests" / "test_calculator.py").read_bytes() != fixture_data["tests_after"]:
            raise RuntimeError("Promoted tests differ from the repaired candidate")
        if (fixture / "PILOT_RESULT.md").read_bytes() != pilot_note:
            raise RuntimeError("Promoted pilot evidence file is missing")

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
                "base_sha": fixture_data["base_sha"],
                "attempt_count": 2,
                "initial_attempt": {
                    "command_id": first_command_id,
                    "session_id": first_session_id,
                    "status": first_result.get("status"),
                    "exit_code": first_result.get("exit_code"),
                    "checkpoint_state": first_result.get("data", {}).get("checkpoint_state"),
                    "rollback_performed": first_result.get("data", {}).get("rollback_performed"),
                    "analysis": analysis.as_dict(),
                    "workspace_clean_after_rollback": True,
                },
                "repair_attempt": {
                    "command_id": second_command_id,
                    "session_id": second_session_id,
                    "status": second_result.get("status"),
                    "exit_code": second_result.get("exit_code"),
                    "checkpoint_state": second_result.get("data", {}).get("checkpoint_state"),
                    "changed_files": second_result.get("changed_files"),
                },
                "promotion": promotion.as_dict(),
                "final_tests": {"exit_code": final_tests.returncode, "stdout_tail": str(final_tests.stdout)[-2000:]},
                "source_checkout_clean": True,
                "source_commit": promotion.source_commit,
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
    parser = argparse.ArgumentParser(description="Run one bounded failure -> repair -> retest -> ff-only promotion pilot.")
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

    report: dict[str, Any] = {
        "schema": "bdb-one-message-repair-pilot-report-v1",
        "status": "failed",
        "started_at": canonical_time(datetime.now(timezone.utc)),
    }
    try:
        report = execute_pilot(repo_root=repo_root, root=root, python_executable=python_executable, timeout=args.timeout)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["completed_at"] = canonical_time(datetime.now(timezone.utc))
        (root / "one-message-repair-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
