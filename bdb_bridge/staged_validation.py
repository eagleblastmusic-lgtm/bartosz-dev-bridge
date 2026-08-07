from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import ProfileRunOutcome


def _targeted_test_paths(workspace_path: Path, changed_paths: Sequence[str]) -> tuple[str, ...]:
    tests_root = workspace_path / "tests"
    selected: set[str] = set()

    def add_path(path: Path) -> None:
        if path.is_file():
            selected.add(path.relative_to(workspace_path).as_posix())

    def add_glob(pattern: str) -> None:
        if tests_root.is_dir():
            for path in tests_root.glob(pattern):
                add_path(path)

    for relative in changed_paths:
        if relative.startswith("tests/") and relative.endswith(".py"):
            add_path(workspace_path / Path(relative))
            continue

        if relative.startswith("browser_extension/"):
            add_glob("test_browser*.py")
            continue

        changed_path = Path(relative)
        migration_contract_changed = (
            relative == "bdb_bridge/migrations.py"
            or (
                relative.startswith("bdb_bridge/")
                and changed_path.stem.endswith("_migration")
            )
        )
        if migration_contract_changed:
            add_glob("test_*migration*.py")
            add_path(tests_root / "test_durable_ingestion_additional.py")
            add_path(tests_root / "test_multi_file_patch_v10_contracts.py")

        if relative.endswith(".py"):
            stem = changed_path.stem
            add_glob(f"test_{stem}.py")
            add_glob(f"test_{stem}_*.py")
            add_glob(f"test_*_{stem}.py")
            if len(changed_path.parts) > 1:
                package_label = changed_path.parts[0]
                if package_label.startswith("bdb_"):
                    package_label = package_label[4:]
                add_glob(f"test_{package_label}*.py")

    return tuple(sorted(selected))


def _run_pytest(
    *,
    workspace_path: Path,
    python_executable: str | Path,
    timeout_seconds: float,
    environment: Mapping[str, str],
    test_paths: Sequence[str],
) -> ProfileRunOutcome:
    command = [str(python_executable), "-m", "pytest", "-q", *test_paths]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace_path,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=dict(environment),
            shell=False,
        )
        return ProfileRunOutcome(
            "success" if completed.returncode == 0 else "failed",
            completed.returncode,
            completed.stdout,
            completed.stderr,
            int((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return ProfileRunOutcome(
            "timeout",
            None,
            stdout,
            stderr,
            int((time.monotonic() - started) * 1000),
        )
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        return ProfileRunOutcome(
            "internal_error",
            None,
            "",
            type(exc).__name__,
            int((time.monotonic() - started) * 1000),
        )


def run_staged_pytest_profile(
    *,
    workspace_path: Path,
    python_executable: str | Path,
    timeout_seconds: float,
    environment: Mapping[str, str],
    changed_paths: Sequence[str],
) -> ProfileRunOutcome:
    started = time.monotonic()

    for relative in changed_paths:
        if not relative.endswith(".py"):
            continue
        candidate = workspace_path / Path(relative)
        if not candidate.is_file():
            continue
        try:
            compile(candidate.read_bytes(), str(candidate), "exec")
        except (SyntaxError, UnicodeError, OSError) as exc:
            return ProfileRunOutcome(
                "failed",
                1,
                "[structural] python_compile failed\n",
                str(exc),
                int((time.monotonic() - started) * 1000),
            )

    targeted = _targeted_test_paths(workspace_path, changed_paths)
    stdout_parts = ["[structural] success\n"]
    stderr_parts: list[str] = []

    if targeted:
        targeted_outcome = _run_pytest(
            workspace_path=workspace_path,
            python_executable=python_executable,
            timeout_seconds=timeout_seconds,
            environment=environment,
            test_paths=targeted,
        )
        stdout_parts.append(
            f"[targeted] status={targeted_outcome.status} duration_ms={targeted_outcome.duration_ms} "
            f"tests={','.join(targeted)}\n"
        )
        stdout_parts.append(targeted_outcome.stdout)
        if targeted_outcome.stderr:
            stderr_parts.append(targeted_outcome.stderr)
        if targeted_outcome.status != "success":
            return ProfileRunOutcome(
                targeted_outcome.status,
                targeted_outcome.exit_code,
                "".join(stdout_parts),
                "".join(stderr_parts),
                int((time.monotonic() - started) * 1000),
            )
    else:
        stdout_parts.append("[targeted] skipped=no_related_tests\n")

    full_outcome = _run_pytest(
        workspace_path=workspace_path,
        python_executable=python_executable,
        timeout_seconds=timeout_seconds,
        environment=environment,
        test_paths=(),
    )
    stdout_parts.append(
        f"[full] status={full_outcome.status} duration_ms={full_outcome.duration_ms}\n"
    )
    stdout_parts.append(full_outcome.stdout)
    if full_outcome.stderr:
        stderr_parts.append(full_outcome.stderr)
    return ProfileRunOutcome(
        full_outcome.status,
        full_outcome.exit_code,
        "".join(stdout_parts),
        "".join(stderr_parts),
        int((time.monotonic() - started) * 1000),
    )


VALIDATION_PLAN_ID = "bdb-pytest-staged-v1"


def _durable_stage(
    *,
    journal: Any,
    command_id: str,
    stage_index: int,
    stage_name: str,
    runner: Any,
) -> ProfileRunOutcome:
    existing = journal.get_validation_run(command_id, VALIDATION_PLAN_ID, stage_index)
    if existing is not None:
        return existing.to_outcome()
    started_at = journal._now_fn()
    outcome = runner()
    finished_at = journal._now_fn()
    return journal.record_validation_run(
        command_id=command_id,
        plan_id=VALIDATION_PLAN_ID,
        stage_index=stage_index,
        stage_name=stage_name,
        outcome=outcome,
        started_at=started_at,
        finished_at=finished_at,
    ).to_outcome()


def run_durable_staged_pytest_profile(
    *,
    journal: Any,
    command_id: str,
    workspace_path: Path,
    python_executable: str | Path,
    timeout_seconds: float,
    environment: Mapping[str, str],
    changed_paths: Sequence[str],
) -> ProfileRunOutcome:
    def structural_runner() -> ProfileRunOutcome:
        started = time.monotonic()
        for relative in changed_paths:
            if not relative.endswith(".py"):
                continue
            candidate = workspace_path / Path(relative)
            if not candidate.is_file():
                continue
            try:
                compile(candidate.read_bytes(), str(candidate), "exec")
            except (SyntaxError, UnicodeError, OSError) as exc:
                return ProfileRunOutcome(
                    "failed",
                    1,
                    "[structural] python_compile failed\n",
                    str(exc),
                    int((time.monotonic() - started) * 1000),
                )
        return ProfileRunOutcome(
            "success",
            0,
            "[structural] success\n",
            "",
            int((time.monotonic() - started) * 1000),
        )

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    duration_ms = 0

    structural = _durable_stage(
        journal=journal,
        command_id=command_id,
        stage_index=1,
        stage_name="structural",
        runner=structural_runner,
    )
    stdout_parts.append(structural.stdout)
    if structural.stderr:
        stderr_parts.append(structural.stderr)
    duration_ms += structural.duration_ms
    if structural.status != "success":
        return ProfileRunOutcome(
            structural.status,
            structural.exit_code,
            "".join(stdout_parts),
            "".join(stderr_parts),
            duration_ms,
        )

    targeted_paths = _targeted_test_paths(workspace_path, changed_paths)

    def targeted_runner() -> ProfileRunOutcome:
        if not targeted_paths:
            return ProfileRunOutcome("success", 0, "[targeted] skipped=no_related_tests\n", "", 0)
        outcome = _run_pytest(
            workspace_path=workspace_path,
            python_executable=python_executable,
            timeout_seconds=timeout_seconds,
            environment=environment,
            test_paths=targeted_paths,
        )
        return ProfileRunOutcome(
            outcome.status,
            outcome.exit_code,
            (
                f"[targeted] status={outcome.status} duration_ms={outcome.duration_ms} "
                f"tests={','.join(targeted_paths)}\n"
                + outcome.stdout
            ),
            outcome.stderr,
            outcome.duration_ms,
        )

    targeted = _durable_stage(
        journal=journal,
        command_id=command_id,
        stage_index=2,
        stage_name="targeted",
        runner=targeted_runner,
    )
    stdout_parts.append(targeted.stdout)
    if targeted.stderr:
        stderr_parts.append(targeted.stderr)
    duration_ms += targeted.duration_ms
    if targeted.status != "success":
        return ProfileRunOutcome(
            targeted.status,
            targeted.exit_code,
            "".join(stdout_parts),
            "".join(stderr_parts),
            duration_ms,
        )

    def full_runner() -> ProfileRunOutcome:
        outcome = _run_pytest(
            workspace_path=workspace_path,
            python_executable=python_executable,
            timeout_seconds=timeout_seconds,
            environment=environment,
            test_paths=(),
        )
        return ProfileRunOutcome(
            outcome.status,
            outcome.exit_code,
            f"[full] status={outcome.status} duration_ms={outcome.duration_ms}\n" + outcome.stdout,
            outcome.stderr,
            outcome.duration_ms,
        )

    full = _durable_stage(
        journal=journal,
        command_id=command_id,
        stage_index=3,
        stage_name="full",
        runner=full_runner,
    )
    stdout_parts.append(full.stdout)
    if full.stderr:
        stderr_parts.append(full.stderr)
    duration_ms += full.duration_ms
    return ProfileRunOutcome(
        full.status,
        full.exit_code,
        "".join(stdout_parts),
        "".join(stderr_parts),
        duration_ms,
    )
