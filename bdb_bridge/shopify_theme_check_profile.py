\
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Sequence

from .models import ProfileRunOutcome


THEME_ROOTS = (
    "assets",
    "blocks",
    "config",
    "layout",
    "locales",
    "sections",
    "snippets",
    "templates",
)
_BLOCKING_SEVERITIES = frozenset({"error", "crash"})
_MAX_REPORTED_NEW_ERRORS = 50
_MAX_DIAGNOSTIC_TAIL = 8000


class ShopifyThemeCheckProfileError(RuntimeError):
    pass


def _tail(value: str, limit: int = _MAX_DIAGNOSTIC_TAIL) -> str:
    return value if len(value) <= limit else value[-limit:]


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("shopify_theme_check", 0)
    return remaining


def _resolved_tool(name: str, environment: Mapping[str, str]) -> str:
    resolved = shutil.which(name, path=environment.get("PATH"))
    if resolved is None:
        raise ShopifyThemeCheckProfileError(f"{name} executable was not found on PATH")
    return str(Path(resolved).resolve(strict=False))


def _run_git(
    workspace: Path,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> subprocess.CompletedProcess[str]:
    command = [_resolved_tool("git", environment), "-C", str(workspace), *arguments]
    completed = subprocess.run(
        command,
        cwd=workspace,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=_remaining_seconds(deadline),
        env=dict(environment),
        shell=False,
    )
    if completed.returncode != 0:
        raise ShopifyThemeCheckProfileError(
            "Git command failed: "
            + " ".join(arguments)
            + "\n"
            + _tail(completed.stderr or completed.stdout)
        )
    return completed


def _assert_repository_root(
    workspace: Path,
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> None:
    completed = _run_git(
        workspace,
        ("rev-parse", "--show-toplevel"),
        environment=environment,
        deadline=deadline,
    )
    reported = Path(completed.stdout.strip()).resolve(strict=False)
    if reported != workspace.resolve(strict=False):
        raise ShopifyThemeCheckProfileError(
            f"Workspace is not the repository root: {workspace}"
        )


def _copy_current_theme(workspace: Path, destination: Path) -> None:
    copied_any = False
    for root_name in THEME_ROOTS:
        source_root = workspace / root_name
        if not source_root.exists():
            continue
        if source_root.is_symlink() or not source_root.is_dir():
            raise ShopifyThemeCheckProfileError(
                f"Theme root must be a real directory: {root_name}"
            )
        copied_any = True
        destination_root = destination / root_name
        destination_root.mkdir(parents=True, exist_ok=True)
        for current, directory_names, file_names in os.walk(
            source_root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            for directory_name in list(directory_names):
                directory_path = current_path / directory_name
                if directory_path.is_symlink():
                    raise ShopifyThemeCheckProfileError(
                        "Symlinked theme directories are not allowed: "
                        + directory_path.relative_to(workspace).as_posix()
                    )
            relative_directory = current_path.relative_to(workspace)
            (destination / relative_directory).mkdir(parents=True, exist_ok=True)
            for file_name in file_names:
                source_file = current_path / file_name
                relative_file = source_file.relative_to(workspace)
                if source_file.is_symlink() or not source_file.is_file():
                    raise ShopifyThemeCheckProfileError(
                        "Theme files must be regular files: "
                        + relative_file.as_posix()
                    )
                target_file = destination / relative_file
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target_file)
    if not copied_any:
        raise ShopifyThemeCheckProfileError("No Shopify theme directories were found")


def _safe_extract_archive(archive_path: Path, destination: Path) -> None:
    destination_resolved = destination.resolve(strict=False)
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive.getmembers():
            pure_name = PurePosixPath(member.name)
            if pure_name.is_absolute() or ".." in pure_name.parts:
                raise ShopifyThemeCheckProfileError(
                    f"Unsafe path in Git archive: {member.name}"
                )
            if member.issym() or member.islnk() or member.isdev():
                raise ShopifyThemeCheckProfileError(
                    f"Unsupported archive entry: {member.name}"
                )
            target = (destination / Path(*pure_name.parts)).resolve(strict=False)
            try:
                target.relative_to(destination_resolved)
            except ValueError as exc:
                raise ShopifyThemeCheckProfileError(
                    f"Archive entry escapes snapshot root: {member.name}"
                ) from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ShopifyThemeCheckProfileError(
                    f"Unsupported archive entry type: {member.name}"
                )
            source = archive.extractfile(member)
            if source is None:
                raise ShopifyThemeCheckProfileError(
                    f"Unable to read archive entry: {member.name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _materialize_head_theme(
    workspace: Path,
    destination: Path,
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> str:
    head = _run_git(
        workspace,
        ("rev-parse", "HEAD"),
        environment=environment,
        deadline=deadline,
    ).stdout.strip()
    existing_roots = tuple(
        line.strip()
        for line in _run_git(
            workspace,
            ("ls-tree", "-d", "--name-only", "HEAD", "--", *THEME_ROOTS),
            environment=environment,
            deadline=deadline,
        ).stdout.splitlines()
        if line.strip() in THEME_ROOTS
    )
    if not existing_roots:
        raise ShopifyThemeCheckProfileError(
            "HEAD contains no Shopify theme directories"
        )

    archive_path = destination.parent / "head-theme.tar"
    _run_git(
        workspace,
        (
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            "HEAD",
            "--",
            *existing_roots,
        ),
        environment=environment,
        deadline=deadline,
    )
    _safe_extract_archive(archive_path, destination)
    return head


@contextmanager
def _theme_snapshots(
    workspace: Path,
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> Iterator[tuple[Path, Path, str]]:
    with tempfile.TemporaryDirectory(prefix="bdb-shopify-theme-check-") as temporary:
        temporary_root = Path(temporary)
        baseline_root = temporary_root / "baseline"
        current_root = temporary_root / "current"
        baseline_root.mkdir()
        current_root.mkdir()
        _assert_repository_root(
            workspace,
            environment=environment,
            deadline=deadline,
        )
        head = _materialize_head_theme(
            workspace,
            baseline_root,
            environment=environment,
            deadline=deadline,
        )
        _copy_current_theme(workspace, current_root)
        yield baseline_root, current_root, head


def _run_theme_check_document(
    command: Sequence[str],
    snapshot_root: Path,
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> tuple[object, int, str]:
    completed = subprocess.run(
        [*command, "--path", str(snapshot_root)],
        cwd=snapshot_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=_remaining_seconds(deadline),
        env=dict(environment),
        shell=False,
    )
    if completed.returncode not in {0, 1}:
        raise ShopifyThemeCheckProfileError(
            f"Shopify Theme Check exited with {completed.returncode}: "
            + _tail(completed.stderr or completed.stdout)
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ShopifyThemeCheckProfileError(
            "Shopify Theme Check did not return valid JSON: "
            + _tail(completed.stdout or completed.stderr)
        ) from exc
    return document, completed.returncode, _tail(completed.stderr)


def _relative_offense_path(value: object, snapshot_root: Path) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "<unknown>"
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve(strict=False).relative_to(
                snapshot_root.resolve(strict=False)
            )
        except ValueError:
            return candidate.name
    return candidate.as_posix().replace("\\", "/")


def _blocking_offenses(document: object, snapshot_root: Path) -> list[dict[str, object]]:
    if isinstance(document, list):
        file_entries = document
    elif isinstance(document, dict) and isinstance(document.get("files"), list):
        file_entries = document["files"]
    elif isinstance(document, dict):
        file_entries = [document]
    else:
        raise ShopifyThemeCheckProfileError(
            "Unexpected Shopify Theme Check JSON structure"
        )

    results: list[dict[str, object]] = []
    for file_entry in file_entries:
        if not isinstance(file_entry, dict):
            continue
        relative_path = _relative_offense_path(
            file_entry.get("path")
            or file_entry.get("file")
            or file_entry.get("filename"),
            snapshot_root,
        )
        offenses = (
            file_entry.get("offenses")
            or file_entry.get("findings")
            or file_entry.get("issues")
            or []
        )
        if not isinstance(offenses, list):
            continue
        for offense in offenses:
            if not isinstance(offense, dict):
                continue
            severity = str(offense.get("severity") or "").strip().lower()
            if severity not in _BLOCKING_SEVERITIES:
                continue
            results.append(
                {
                    "path": relative_path,
                    "check": str(
                        offense.get("check")
                        or offense.get("code")
                        or offense.get("rule")
                        or "<unknown>"
                    ),
                    "message": str(
                        offense.get("message")
                        or offense.get("description")
                        or offense.get("detail")
                        or ""
                    ),
                    "severity": severity,
                    "start_row": offense.get("start_row")
                    or offense.get("line")
                    or (
                        offense.get("location", {})
                        .get("start", {})
                        .get("line")
                        if isinstance(offense.get("location"), dict)
                        else None
                    ),
                }
            )
    return results


def _fingerprint(offense: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(offense["path"]),
        str(offense["check"]),
        str(offense["message"]),
    )


def compare_theme_check_documents(
    baseline_document: object,
    current_document: object,
    *,
    baseline_root: Path,
    current_root: Path,
) -> dict[str, object]:
    baseline = _blocking_offenses(baseline_document, baseline_root)
    current = _blocking_offenses(current_document, current_root)
    baseline_counts = Counter(_fingerprint(item) for item in baseline)
    current_counts = Counter(_fingerprint(item) for item in current)

    details_by_fingerprint: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for item in current:
        details_by_fingerprint.setdefault(_fingerprint(item), []).append(item)

    new_errors: list[dict[str, object]] = []
    for fingerprint in sorted(current_counts):
        additional = current_counts[fingerprint] - baseline_counts[fingerprint]
        if additional <= 0:
            continue
        new_errors.extend(details_by_fingerprint[fingerprint][:additional])

    return {
        "baseline_blocking_errors": len(baseline),
        "current_blocking_errors": len(current),
        "new_blocking_errors": len(new_errors),
        "new_errors": new_errors[:_MAX_REPORTED_NEW_ERRORS],
        "new_errors_truncated": len(new_errors) > _MAX_REPORTED_NEW_ERRORS,
    }


def run_shopify_theme_check_profile(
    *,
    workspace_path: str | Path,
    command: Sequence[str],
    timeout_seconds: float,
    environment: Mapping[str, str],
) -> ProfileRunOutcome:
    started = time.monotonic()
    deadline = started + float(timeout_seconds)
    workspace = Path(workspace_path).resolve(strict=False)
    try:
        with _theme_snapshots(
            workspace,
            environment=environment,
            deadline=deadline,
        ) as (baseline_root, current_root, head):
            baseline_document, baseline_exit, baseline_stderr = (
                _run_theme_check_document(
                    command,
                    baseline_root,
                    environment=environment,
                    deadline=deadline,
                )
            )
            current_document, current_exit, current_stderr = (
                _run_theme_check_document(
                    command,
                    current_root,
                    environment=environment,
                    deadline=deadline,
                )
            )
            comparison = compare_theme_check_documents(
                baseline_document,
                current_document,
                baseline_root=baseline_root,
                current_root=current_root,
            )
        failed = int(comparison["new_blocking_errors"]) > 0
        summary = {
            "schema": "bdb-shopify-theme-check-profile-v1",
            "profile_id": "shopify_theme_check",
            "status": "failed" if failed else "success",
            "baseline_head": head,
            "baseline_exit_code": baseline_exit,
            "current_exit_code": current_exit,
            **comparison,
        }
        stderr_parts = [
            value
            for value in (
                f"baseline: {baseline_stderr}" if baseline_stderr else "",
                f"current: {current_stderr}" if current_stderr else "",
            )
            if value
        ]
        return ProfileRunOutcome(
            "failed" if failed else "success",
            1 if failed else 0,
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
            _tail("\n".join(stderr_parts)),
            int((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        return ProfileRunOutcome(
            "timeout",
            None,
            "",
            f"Shopify Theme Check timed out after {timeout_seconds:g} seconds",
            int((time.monotonic() - started) * 1000),
        )
    except (
        FileNotFoundError,
        OSError,
        UnicodeError,
        ShopifyThemeCheckProfileError,
        tarfile.TarError,
    ) as exc:
        return ProfileRunOutcome(
            "internal_error",
            None,
            "",
            _tail(f"{type(exc).__name__}: {exc}"),
            int((time.monotonic() - started) * 1000),
        )
