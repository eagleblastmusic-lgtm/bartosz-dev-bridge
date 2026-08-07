from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

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

        if relative.endswith(".py"):
            changed_path = Path(relative)
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
