from __future__ import annotations

import argparse
import io
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from bdb_bridge.command_timing import build_command_timing
from bdb_bridge.journal import Journal
from bdb_bridge.native_messaging import encode_native_message, read_native_message

SCHEMA = "bdb-local-browser-benchmark-v1"
PILOT_REPOSITORY_ID = "bdb-local-browser-pilot"
PILOT_ALIAS = "pilot"
PILOT_ALLOWED_PATHS = ["src/clamp.py", "tests/test_clamp.py", "PILOT_RESULT.md"]
SUITE_OPEN_READ = "warm_open_read"
SUITE_PATCH = "warm_multi_file_patch"
SUITE_COLD = "cold_start_open_read"

TARGETS_MS: dict[str, dict[str, dict[str, float]]] = {
    SUITE_OPEN_READ: {
        "native_roundtrip_ms": {"p50": 1000.0, "p95": 2000.0},
    },
    SUITE_PATCH: {
        "native_roundtrip_ms": {"p50": 3000.0, "p95": 5000.0},
    },
    SUITE_COLD: {
        "cold_total_ms": {"p50": 5000.0, "p95": 10000.0},
    },
    "all_successful_runs": {
        "result_publication_ms": {"p50": 500.0, "p95": 1000.0},
    },
}


@dataclass(frozen=True)
class HarnessPaths:
    root: Path
    operator_state: Path
    bridge_config: Path
    native_config: Path
    native_manifest: Path
    python_executable: Path
    host_executable: Path
    fixture_repo: Path
    journal_path: Path
    read_action: Path
    patch_action: Path
    origin: str
    max_wait_seconds: float


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def run_checked(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def git(repo: Path, *args: str) -> str:
    return run_checked(["git", "-C", str(repo), *args]).stdout.strip()


def enum_value(value: object) -> object:
    return getattr(value, "value", value)


def require_clean_checkout(path: Path, *, label: str) -> None:
    status = git(path, "status", "--porcelain=v1")
    if status:
        raise RuntimeError(f"{label} must be clean before benchmarking:\n{status}")


def _require_path(value: object, *, label: str, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a non-empty path")
    path = Path(value).expanduser().resolve(strict=False)
    if must_exist and not path.exists():
        raise RuntimeError(f"{label} does not exist: {path}")
    return path


def validate_environment(root: Path) -> HarnessPaths:
    root = root.expanduser().resolve(strict=True)
    operator_state = root / "operator-state.json"
    state = load_json(operator_state)
    if state.get("schema") != "bdb-local-browser-pilot-operator-state-v1":
        raise RuntimeError("Unsupported or missing local browser pilot operator state")
    if state.get("repo_alias") != PILOT_ALIAS:
        raise RuntimeError("Benchmark is restricted to the synthetic 'pilot' repository alias")
    if _require_path(state.get("root"), label="operator root") != root:
        raise RuntimeError("Operator state root does not match --root")

    bridge_config = _require_path(state.get("bridge_config"), label="bridge config")
    native_config = _require_path(state.get("native_config"), label="native config")
    native_manifest = _require_path(state.get("native_manifest"), label="native manifest")
    python_executable = _require_path(state.get("python_executable"), label="python executable")
    read_action = _require_path(state.get("read_action"), label="read action")
    patch_action = _require_path(state.get("patch_action"), label="patch action")

    bridge = load_json(bridge_config)
    if bridge.get("repository_id") != PILOT_REPOSITORY_ID:
        raise RuntimeError("Benchmark refuses any repository other than bdb-local-browser-pilot")
    if bridge.get("allowed_paths") != PILOT_ALLOWED_PATHS:
        raise RuntimeError("Synthetic pilot allowlist does not match the exact three-path contract")
    if bridge.get("direct_spool_enabled") is not True:
        raise RuntimeError("Direct spool must remain enabled for this benchmark")
    fixture_repo = _require_path(bridge.get("fixture_repo_path"), label="fixture repository")
    journal_path = _require_path(bridge.get("journal_path"), label="Journal", must_exist=False)

    native = load_json(native_config)
    repositories = native.get("repositories")
    if not isinstance(repositories, dict) or set(repositories) != {PILOT_ALIAS}:
        raise RuntimeError("Native Host must expose exactly the synthetic 'pilot' alias")
    repository = repositories[PILOT_ALIAS]
    if not isinstance(repository, dict):
        raise RuntimeError("Synthetic Native Host alias has an invalid definition")
    configured_bridge = _require_path(
        repository.get("bridge_config_path"),
        label="Native Host bridge config",
    )
    if configured_bridge != bridge_config:
        raise RuntimeError("Native Host alias points at a different Bridge configuration")

    extension_id = state.get("extension_id")
    if not isinstance(extension_id, str) or len(extension_id) != 32:
        raise RuntimeError("Pilot extension id is missing or invalid")
    origin = f"chrome-extension://{extension_id}/"
    allowed_origins = native.get("allowed_origins")
    if not isinstance(allowed_origins, list) or origin not in allowed_origins:
        raise RuntimeError("Pilot extension origin is not allowed by Native Host")

    manifest = load_json(native_manifest)
    if manifest.get("name") != "com.bartosz.dev_bridge":
        raise RuntimeError("Unexpected Native Host manifest")
    if manifest.get("allowed_origins") != allowed_origins:
        raise RuntimeError("Native Host manifest/config origin sets differ")
    host_executable = _require_path(manifest.get("path"), label="Native Host executable")

    max_wait_seconds = native.get("max_wait_seconds", 30.0)
    if isinstance(max_wait_seconds, bool) or not isinstance(max_wait_seconds, (int, float)):
        raise RuntimeError("Native Host max_wait_seconds must be numeric")
    max_wait_seconds = float(max_wait_seconds)
    if not 0.0 < max_wait_seconds <= 120.0:
        raise RuntimeError("Native Host max_wait_seconds is outside the supported range")

    for action_path, expected_operation in (
        (read_action, "open_read"),
        (patch_action, "multi_file_patch"),
    ):
        action = load_json(action_path)
        if (
            action.get("schema") != "bdb-action-v1"
            or action.get("repo_alias") != PILOT_ALIAS
            or action.get("operation") != expected_operation
            or action.get("expected_revision") != 0
        ):
            raise RuntimeError(f"Benchmark action contract mismatch: {action_path}")

    require_clean_checkout(REPOSITORY_ROOT, label="Bridge checkout")
    require_clean_checkout(fixture_repo, label="Synthetic fixture checkout")

    return HarnessPaths(
        root=root,
        operator_state=operator_state,
        bridge_config=bridge_config,
        native_config=native_config,
        native_manifest=native_manifest,
        python_executable=python_executable,
        host_executable=host_executable,
        fixture_repo=fixture_repo,
        journal_path=journal_path,
        read_action=read_action,
        patch_action=patch_action,
        origin=origin,
        max_wait_seconds=max_wait_seconds,
    )


def bridge_json(paths: HarnessPaths, *arguments: str) -> dict[str, Any]:
    completed = run_checked(
        [
            str(paths.python_executable),
            "-m",
            "bdb_bridge",
            "bridge",
            *arguments,
            "--config",
            str(paths.bridge_config),
            "--json",
        ]
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Bridge command did not return a JSON object")
    return value


def bridge_status(paths: HarnessPaths) -> dict[str, Any]:
    return bridge_json(paths, "status")


def wait_for_bridge(paths: HarnessPaths, expected: str, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = bridge_status(paths)
        if last.get("status") == expected:
            return last
        time.sleep(0.05)
    raise RuntimeError(f"Bridge did not reach {expected}; last={last}")


def native_control(paths: HarnessPaths, action: str, *extra: str) -> dict[str, Any]:
    completed = run_checked(
        [
            str(paths.python_executable),
            "-m",
            "bdb_bridge",
            "bridge",
            "native-host",
            action,
            "--config",
            str(paths.native_config),
            *extra,
        ]
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Native Host control command did not return a JSON object")
    return value


def start_bridge(paths: HarnessPaths) -> float:
    started = time.perf_counter()
    run_checked(
        [
            str(paths.python_executable),
            "-m",
            "bdb_bridge",
            "bridge",
            "start",
            "--config",
            str(paths.bridge_config),
            "--background",
        ]
    )
    wait_for_bridge(paths, "RUNNING")
    return round((time.perf_counter() - started) * 1000.0, 3)


def stop_bridge(paths: HarnessPaths) -> None:
    try:
        native_control(paths, "disarm")
    except Exception:
        pass
    status = bridge_status(paths)
    if status.get("status") == "OFFLINE":
        return
    run_checked(
        [
            str(paths.python_executable),
            "-m",
            "bdb_bridge",
            "bridge",
            "stop",
            "--config",
            str(paths.bridge_config),
        ]
    )
    wait_for_bridge(paths, "OFFLINE")


def arm_native_host(paths: HarnessPaths, *, minutes: int = 60) -> dict[str, Any]:
    return native_control(paths, "arm", "--minutes", str(minutes))


def send_native_request(
    paths: HarnessPaths,
    request: dict[str, Any],
    *,
    process_timeout_seconds: float,
) -> dict[str, Any]:
    command = [
        str(paths.host_executable),
        paths.origin,
        "--config",
        str(paths.native_config),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        stdout, stderr = process.communicate(
            input=encode_native_message(request),
            timeout=process_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(
            f"Native Host process timed out; stdout={stdout!r} stderr={stderr!r}"
        )
    if process.returncode != 0:
        raise RuntimeError(
            f"Native Host exited with {process.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')}"
        )
    response = read_native_message(io.BytesIO(stdout))
    if response is None:
        raise RuntimeError(
            "Native Host returned no framed response: "
            f"{stderr.decode('utf-8', errors='replace')}"
        )
    return response


def completed_native_action(
    paths: HarnessPaths,
    action: dict[str, Any],
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    deadline = time.monotonic() + timeout_seconds
    request = {
        "schema": "bdb-native-request-v1",
        "request_id": f"bench-submit-{uuid.uuid4().hex}",
        "action": "submit_action",
        "bdb_action": action,
        "wait_seconds": min(paths.max_wait_seconds, timeout_seconds),
    }
    response = send_native_request(
        paths,
        request,
        process_timeout_seconds=min(paths.max_wait_seconds, timeout_seconds) + 10.0,
    )
    while response.get("status") in {"accepted", "pending"}:
        command_id = response.get("command_id")
        if not isinstance(command_id, str) or ":" not in command_id:
            raise RuntimeError(f"Native Host did not return a command id: {response}")
        session_id, sequence_text = command_id.rsplit(":", 1)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Timed out waiting for result: {command_id}")
        response = send_native_request(
            paths,
            {
                "schema": "bdb-native-request-v1",
                "request_id": f"bench-result-{uuid.uuid4().hex}",
                "action": "result",
                "repo_alias": PILOT_ALIAS,
                "session_id": session_id,
                "sequence": int(sequence_text),
                "wait_seconds": min(paths.max_wait_seconds, remaining),
            },
            process_timeout_seconds=min(paths.max_wait_seconds, remaining) + 10.0,
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    if response.get("status") == "failed":
        raise RuntimeError(f"Native Host rejected benchmark action: {response}")
    if response.get("status") != "completed":
        raise RuntimeError(f"Unexpected Native Host response: {response}")
    return response, elapsed_ms


def journal_snapshot(
    paths: HarnessPaths,
    command_id: str,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        journal = Journal.open(paths.journal_path)
        try:
            command = journal.get_command(command_id)
            result = journal.get_result(command_id)
            outbox = journal.get_outbox(command_id)
            session = journal.get_session(command.session_id) if command is not None else None
            timing = build_command_timing(journal, command_id) if command is not None else None
            last = {
                "command_state": enum_value(command.state) if command is not None else None,
                "session_state": enum_value(session.state) if session is not None else None,
                "result_status": result.status if result is not None else None,
                "result_error_code": result.error_code if result is not None else None,
                "outbox_state": enum_value(outbox.state) if outbox is not None else None,
                "outbox_attempt_count": outbox.attempt_count if outbox is not None else None,
                "outbox_last_error": outbox.last_error if outbox is not None else None,
                "timing": timing,
            }
        finally:
            journal.close()
        if (
            last["result_status"] is not None
            and last["outbox_state"] == "published"
            and last["timing"]["timestamps"]["result_published_at"] is not None
        ):
            return last
        time.sleep(0.05)
    raise RuntimeError(f"Journal publication did not settle for {command_id}; last={last}")


def run_action(
    paths: HarnessPaths,
    action: dict[str, Any],
    *,
    suite: str,
    index: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    started_at = utc_now()
    response, native_roundtrip_ms = completed_native_action(
        paths,
        action,
        timeout_seconds=timeout_seconds,
    )
    command_id = response.get("command_id")
    result = response.get("result")
    if not isinstance(command_id, str) or not isinstance(result, dict):
        raise RuntimeError(f"Completed response is incomplete: {response}")
    snapshot = journal_snapshot(paths, command_id)
    session_id, sequence_text = command_id.rsplit(":", 1)
    result_status = result.get("status")
    exit_code = result.get("exit_code")
    success = (
        result_status == "success"
        and exit_code == 0
        and snapshot["result_status"] == "success"
        and snapshot["result_error_code"] is None
        and snapshot["outbox_state"] == "published"
        and snapshot["outbox_last_error"] is None
    )
    if not success:
        raise RuntimeError(
            f"Command did not complete successfully: result={result} snapshot={snapshot}"
        )
    timing = snapshot["timing"]
    return {
        "suite": suite,
        "index": index,
        "operation": action["operation"],
        "status": "success",
        "started_at": started_at,
        "finished_at": utc_now(),
        "command_id": command_id,
        "session_id": session_id,
        "sequence": int(sequence_text),
        "native_roundtrip_ms": native_roundtrip_ms,
        "result_duration_ms": result.get("duration_ms"),
        "profile_duration_ms": (
            result.get("data", {}).get("profile_duration_ms")
            if isinstance(result.get("data"), dict)
            else None
        ),
        "changed_files": result.get("changed_files"),
        "command_state": snapshot["command_state"],
        "session_state": snapshot["session_state"],
        "outbox_attempt_count": snapshot["outbox_attempt_count"],
        "timing": timing,
    }


def failed_run(suite: str, index: int, operation: str, exc: Exception) -> dict[str, Any]:
    return {
        "suite": suite,
        "index": index,
        "operation": operation,
        "status": "failed",
        "finished_at": utc_now(),
        "error": f"{type(exc).__name__}: {exc}",
    }


def nearest_rank(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0.0 < percentile <= 100.0:
        raise ValueError("percentile must be in (0, 100]")
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def metric_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    materialized = [float(value) for value in values]
    if not materialized:
        return {
            "count": 0,
            "min": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(materialized),
        "min": round(min(materialized), 3),
        "mean": round(statistics.fmean(materialized), 3),
        "p50": nearest_rank(materialized, 50.0),
        "p95": nearest_rank(materialized, 95.0),
        "max": round(max(materialized), 3),
    }


def nested_metric(run: dict[str, Any], metric: str) -> float | None:
    direct = run.get(metric)
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        return float(direct)
    timing = run.get("timing")
    if not isinstance(timing, dict):
        return None
    durations = timing.get("durations_ms")
    if not isinstance(durations, dict):
        return None
    value = durations.get(metric)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    suites = [SUITE_OPEN_READ, SUITE_PATCH, SUITE_COLD]
    result: dict[str, Any] = {}
    metrics = (
        "native_roundtrip_ms",
        "cold_start_to_running_ms",
        "cold_total_ms",
        "end_to_end_ms",
        "execution_ms",
        "result_publication_ms",
    )
    for suite in suites:
        suite_runs = [run for run in runs if run.get("suite") == suite]
        successful = [run for run in suite_runs if run.get("status") == "success"]
        result[suite] = {
            "runs": len(suite_runs),
            "successes": len(successful),
            "failures": len(suite_runs) - len(successful),
            "metrics_ms": {
                metric: metric_summary(
                    value
                    for run in successful
                    if (value := nested_metric(run, metric)) is not None
                )
                for metric in metrics
            },
        }
    all_successful = [run for run in runs if run.get("status") == "success"]
    result["all_successful_runs"] = {
        "runs": len(all_successful),
        "metrics_ms": {
            "result_publication_ms": metric_summary(
                value
                for run in all_successful
                if (value := nested_metric(run, "result_publication_ms")) is not None
            )
        },
    }
    return result


def evaluate_targets(summary: dict[str, Any]) -> list[dict[str, Any]]:
    evaluations: list[dict[str, Any]] = []
    for suite, metrics in TARGETS_MS.items():
        for metric, limits in metrics.items():
            observed = summary[suite]["metrics_ms"][metric]
            for percentile, limit in limits.items():
                value = observed.get(percentile)
                evaluations.append(
                    {
                        "suite": suite,
                        "metric": metric,
                        "percentile": percentile,
                        "limit_ms": limit,
                        "observed_ms": value,
                        "met": value is not None and value <= limit,
                    }
                )
    return evaluations


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Bartosz Dev Bridge — local performance benchmark",
        "",
        f"- Status: **{report['status']}**",
        f"- Started: `{report['started_at']}`",
        f"- Finished: `{report['finished_at']}`",
        f"- Implementation HEAD: `{report['environment']['implementation_head']}`",
        f"- Synthetic repository: `{report['environment']['repository_id']}`",
        "",
        "## Scope",
        "",
        "The automated series measures Native Host process startup and request/response, "
        "Direct Lane ingestion, Bridge scheduling, isolated worktree execution, test profile, "
        "durable staging and result publication.",
        "",
        "It does **not** measure ChatGPT answer generation, DOM streaming, a human click, "
        "or browser rendering time.",
        "",
        "## Suites",
        "",
        "| Suite | Runs | Success | Failure | Main metric | p50 | p95 | Max |",
        "|---|---:|---:|---:|---|---:|---:|---:|",
    ]
    main_metric = {
        SUITE_OPEN_READ: "native_roundtrip_ms",
        SUITE_PATCH: "native_roundtrip_ms",
        SUITE_COLD: "cold_total_ms",
    }
    for suite in (SUITE_OPEN_READ, SUITE_PATCH, SUITE_COLD):
        item = report["summary"][suite]
        metric_name = main_metric[suite]
        metric = item["metrics_ms"][metric_name]
        lines.append(
            f"| `{suite}` | {item['runs']} | {item['successes']} | {item['failures']} | "
            f"`{metric_name}` | {metric['p50']} | {metric['p95']} | {metric['max']} |"
        )
    lines.extend(
        [
            "",
            "## Performance targets",
            "",
            "| Suite | Metric | Percentile | Limit (ms) | Observed (ms) | Met |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for target in report["targets"]:
        lines.append(
            f"| `{target['suite']}` | `{target['metric']}` | {target['percentile']} | "
            f"{target['limit_ms']} | {target['observed_ms']} | "
            f"{'YES' if target['met'] else 'NO'} |"
        )
    failures = [run for run in report["runs"] if run.get("status") != "success"]
    if failures:
        lines.extend(["", "## Failures", ""])
        for failure in failures:
            lines.append(
                f"- `{failure['suite']}#{failure['index']}`: {failure.get('error', 'unknown error')}"
            )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Exact synthetic repository id and three-path allowlist required.",
            "- Bridge and fixture checkouts must be clean.",
            "- Benchmark must begin with Bridge OFFLINE.",
            "- Every mutation runs in a fresh isolated session worktree.",
            "- Source fixture cleanliness is verified again at the end.",
            "- Native Host is disarmed and Bridge is returned to OFFLINE.",
            "- No repository, worktree, Journal, result or registration is deleted.",
            "",
        ]
    )
    return "\n".join(lines)


def record_series(
    paths: HarnessPaths,
    action: dict[str, Any],
    *,
    suite: str,
    count: int,
    timeout_seconds: float,
    runs: list[dict[str, Any]],
) -> None:
    for index in range(1, count + 1):
        print(f"[{suite}] {index}/{count}", flush=True)
        try:
            runs.append(
                run_action(
                    paths,
                    action,
                    suite=suite,
                    index=index,
                    timeout_seconds=timeout_seconds,
                )
            )
        except Exception as exc:
            runs.append(failed_run(suite, index, str(action.get("operation")), exc))


def parse_args() -> argparse.Namespace:
    default_root = (
        Path(os.environ["LOCALAPPDATA"]) / "BartoszDevBridge" / "local-browser-pilot"
        if os.environ.get("LOCALAPPDATA")
        else None
    )
    parser = argparse.ArgumentParser(
        description="Benchmark the preserved synthetic Bartosz Dev Bridge local browser pilot."
    )
    parser.add_argument(
        "--root",
        default=str(default_root) if default_root else None,
        required=default_root is None,
    )
    parser.add_argument("--open-read-runs", type=int, default=20)
    parser.add_argument("--patch-runs", type=int, default=10)
    parser.add_argument("--cold-start-runs", type=int, default=5)
    parser.add_argument("--operation-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--fail-on-target-miss", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for label in ("open_read_runs", "patch_runs", "cold_start_runs"):
        if getattr(args, label) < 0:
            raise RuntimeError(f"{label} must be non-negative")
    if args.open_read_runs + args.patch_runs + args.cold_start_runs <= 0:
        raise RuntimeError("At least one benchmark run is required")
    if not 1.0 <= args.operation_timeout_seconds <= 300.0:
        raise RuntimeError("operation timeout must be between 1 and 300 seconds")

    paths = validate_environment(Path(args.root))
    initial_status = bridge_status(paths)
    if initial_status.get("status") != "OFFLINE":
        raise RuntimeError(
            "Benchmark requires Bridge OFFLINE at start; stop the pilot before running it"
        )

    benchmark_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = paths.root / "benchmarks" / benchmark_id
    output_dir.mkdir(parents=True, exist_ok=False)
    read_action = load_json(paths.read_action)
    patch_action = load_json(paths.patch_action)
    runs: list[dict[str, Any]] = []
    started_at = utc_now()

    try:
        if args.open_read_runs or args.patch_runs:
            print("Starting warm benchmark service...", flush=True)
            start_bridge(paths)
            arm_native_host(paths)
            record_series(
                paths,
                read_action,
                suite=SUITE_OPEN_READ,
                count=args.open_read_runs,
                timeout_seconds=args.operation_timeout_seconds,
                runs=runs,
            )
            record_series(
                paths,
                patch_action,
                suite=SUITE_PATCH,
                count=args.patch_runs,
                timeout_seconds=args.operation_timeout_seconds,
                runs=runs,
            )
            stop_bridge(paths)

        for index in range(1, args.cold_start_runs + 1):
            print(f"[{SUITE_COLD}] {index}/{args.cold_start_runs}", flush=True)
            cold_started = time.perf_counter()
            try:
                start_to_running_ms = start_bridge(paths)
                arm_native_host(paths)
                run = run_action(
                    paths,
                    read_action,
                    suite=SUITE_COLD,
                    index=index,
                    timeout_seconds=args.operation_timeout_seconds,
                )
                run["cold_start_to_running_ms"] = start_to_running_ms
                run["cold_total_ms"] = round(
                    (time.perf_counter() - cold_started) * 1000.0,
                    3,
                )
                runs.append(run)
            except Exception as exc:
                runs.append(failed_run(SUITE_COLD, index, "open_read", exc))
            finally:
                stop_bridge(paths)
    finally:
        try:
            stop_bridge(paths)
        finally:
            require_clean_checkout(paths.fixture_repo, label="Synthetic fixture checkout")

    summary = summarize_runs(runs)
    targets = evaluate_targets(summary)
    failures = sum(1 for run in runs if run.get("status") != "success")
    targets_met = all(target["met"] for target in targets)
    status = "failed" if failures else ("pass" if targets_met else "target_miss")
    report = {
        "schema": SCHEMA,
        "benchmark_id": benchmark_id,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "configuration": {
            "open_read_runs": args.open_read_runs,
            "patch_runs": args.patch_runs,
            "cold_start_runs": args.cold_start_runs,
            "operation_timeout_seconds": args.operation_timeout_seconds,
            "percentile_method": "nearest-rank",
        },
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "implementation_head": git(REPOSITORY_ROOT, "rev-parse", "HEAD"),
            "setup_implementation_sha": load_json(paths.operator_state).get("implementation_sha"),
            "repository_id": PILOT_REPOSITORY_ID,
            "repo_alias": PILOT_ALIAS,
            "root": str(paths.root),
            "bridge_config": str(paths.bridge_config),
            "journal": str(paths.journal_path),
        },
        "summary": summary,
        "targets": targets,
        "runs": runs,
        "safety": {
            "source_fixture_clean": True,
            "final_bridge_status": bridge_status(paths).get("status"),
            "artifacts_preserved": True,
            "native_registration_preserved": True,
        },
    }
    json_path = output_dir / "benchmark.json"
    markdown_path = output_dir / "benchmark.md"
    write_json(json_path, report)
    markdown_path.write_text(markdown_report(report), encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "status": status,
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
                "failures": failures,
                "targets_met": targets_met,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if failures:
        return 1
    if args.fail_on_target_miss and not targets_met:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
