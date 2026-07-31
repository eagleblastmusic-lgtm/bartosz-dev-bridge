from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
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



_SCOPED_EXCLUDED_THEME_CHECKS = frozenset(
    {
        "MatchingTranslations",
        "MissingAsset",
        "OrphanedSnippet",
        "TranslationKeyExists",
    }
)
_MAX_SCOPED_PATHS = 50


def _normalize_scoped_paths(
    workspace: Path,
    changed_paths: Sequence[str],
) -> tuple[str, ...]:
    workspace_resolved = workspace.resolve(strict=False)
    normalized: list[str] = []
    for raw in changed_paths:
        if not isinstance(raw, str):
            raise ShopifyThemeCheckProfileError("Changed path must be a string")
        value = raw.replace("\\", "/").strip()
        pure = PurePosixPath(value)
        if (
            not value
            or pure.is_absolute()
            or value.startswith("/")
            or ".." in pure.parts
            or "." in pure.parts
        ):
            raise ShopifyThemeCheckProfileError(f"Unsafe changed path: {raw!r}")
        candidate = (workspace / Path(*pure.parts)).resolve(strict=False)
        try:
            candidate.relative_to(workspace_resolved)
        except ValueError as exc:
            raise ShopifyThemeCheckProfileError(
                f"Changed path escapes workspace: {value}"
            ) from exc
        normalized.append(pure.as_posix())
    unique = tuple(sorted(set(normalized)))
    if not unique:
        raise ShopifyThemeCheckProfileError(
            "Scoped validation requires at least one changed path"
        )
    if len(unique) > _MAX_SCOPED_PATHS:
        raise ShopifyThemeCheckProfileError(
            f"Scoped validation supports at most {_MAX_SCOPED_PATHS} changed paths"
        )
    return unique


def _head_file_bytes(
    workspace: Path,
    relative_path: str,
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> bytes | None:
    command = [
        _resolved_tool("git", environment),
        "-C",
        str(workspace),
        "show",
        f"HEAD:{relative_path}",
    ]
    completed = subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        check=False,
        timeout=_remaining_seconds(deadline),
        env=dict(environment),
        shell=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    detail = completed.stderr.decode("utf-8", errors="replace")
    missing_markers = (
        "does not exist in",
        "exists on disk, but not in",
        "Path '" + relative_path + "' does not exist",
        "fatal: path '" + relative_path + "'",
    )
    if completed.returncode == 128 and any(marker in detail for marker in missing_markers):
        return None
    raise ShopifyThemeCheckProfileError(
        f"Unable to read HEAD:{relative_path}: {_tail(detail)}"
    )


def _current_file_bytes(workspace: Path, relative_path: str) -> bytes | None:
    candidate = workspace / Path(*PurePosixPath(relative_path).parts)
    if not candidate.exists():
        return None
    if candidate.is_symlink() or not candidate.is_file():
        raise ShopifyThemeCheckProfileError(
            f"Changed path must be a regular file or a deletion: {relative_path}"
        )
    return candidate.read_bytes()


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_error(data: bytes | None) -> str | None:
    if data is None:
        return None

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        text = data.decode("utf-8-sig", errors="strict")
        json.loads(text, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _toml_error(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        tomllib.loads(data.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _python_error(data: bytes | None, relative_path: str) -> str | None:
    if data is None:
        return None
    try:
        source = data.decode("utf-8-sig", errors="strict")
        compile(source, relative_path, "exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _utf8_error(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _node_syntax_error(
    data: bytes | None,
    relative_path: str,
    *,
    environment: Mapping[str, str],
    deadline: float,
) -> str | None:
    if data is None:
        return None
    node = _resolved_tool("node", environment)
    suffix = Path(relative_path).suffix or ".js"
    with tempfile.TemporaryDirectory(prefix="bdb-node-check-") as temporary:
        candidate = Path(temporary) / f"candidate{suffix}"
        candidate.write_bytes(data)
        completed = subprocess.run(
            [node, "--check", str(candidate)],
            cwd=candidate.parent,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=_remaining_seconds(deadline),
            env=dict(environment),
            shell=False,
        )
    if completed.returncode == 0:
        return None
    return _tail(completed.stderr or completed.stdout)


def _single_file_validation_error(
    *,
    relative_path: str,
    data: bytes | None,
    environment: Mapping[str, str],
    deadline: float,
) -> tuple[str, str | None]:
    suffix = Path(relative_path).suffix.lower()
    name = Path(relative_path).name.lower()
    if suffix == ".json":
        return "json_parse", _strict_json_error(data)
    if suffix == ".toml" or name == "shopify.theme.toml":
        return "toml_parse", _toml_error(data)
    if suffix == ".py":
        return "python_compile", _python_error(data, relative_path)
    if suffix in {".js", ".mjs", ".cjs"}:
        return (
            "node_syntax",
            _node_syntax_error(
                data,
                relative_path,
                environment=environment,
                deadline=deadline,
            ),
        )
    if suffix in {
        ".css",
        ".scss",
        ".html",
        ".htm",
        ".md",
        ".txt",
        ".ts",
        ".tsx",
        ".jsx",
        ".xml",
        ".svg",
        ".yml",
        ".yaml",
    }:
        return "utf8_decode", _utf8_error(data)
    return "regular_file", None


def _write_snapshot_file(root: Path, relative_path: str, data: bytes) -> None:
    target = root / Path(*PurePosixPath(relative_path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _scoped_theme_offenses(
    document: object,
    snapshot_root: Path,
    allowed_paths: frozenset[str],
) -> list[dict[str, object]]:
    return [
        offense
        for offense in _blocking_offenses(document, snapshot_root)
        if str(offense["path"]) in allowed_paths
        and str(offense["check"]) not in _SCOPED_EXCLUDED_THEME_CHECKS
    ]


def _compare_scoped_theme_documents(
    baseline_document: object,
    current_document: object,
    *,
    baseline_root: Path,
    current_root: Path,
    allowed_paths: frozenset[str],
) -> dict[str, object]:
    baseline = _scoped_theme_offenses(
        baseline_document,
        baseline_root,
        allowed_paths,
    )
    current = _scoped_theme_offenses(
        current_document,
        current_root,
        allowed_paths,
    )
    baseline_counts = Counter(_fingerprint(item) for item in baseline)
    current_counts = Counter(_fingerprint(item) for item in current)
    details_by_fingerprint: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for item in current:
        details_by_fingerprint.setdefault(_fingerprint(item), []).append(item)

    new_errors: list[dict[str, object]] = []
    for fingerprint in sorted(current_counts):
        additional = current_counts[fingerprint] - baseline_counts[fingerprint]
        if additional > 0:
            new_errors.extend(details_by_fingerprint[fingerprint][:additional])

    ignored_existing = sum(
        min(baseline_counts[fingerprint], current_counts[fingerprint])
        for fingerprint in set(baseline_counts) | set(current_counts)
    )
    return {
        "baseline_scoped_errors": len(baseline),
        "current_scoped_errors": len(current),
        "ignored_existing_errors": ignored_existing,
        "new_errors": new_errors[:_MAX_REPORTED_NEW_ERRORS],
        "new_errors_truncated": len(new_errors) > _MAX_REPORTED_NEW_ERRORS,
    }


def _run_scoped_minimal_profile(
    *,
    workspace: Path,
    command: Sequence[str],
    timeout_seconds: float,
    environment: Mapping[str, str],
    changed_paths: Sequence[str],
    started: float,
) -> ProfileRunOutcome:
    deadline = started + float(timeout_seconds)
    paths = _normalize_scoped_paths(workspace, changed_paths)
    checks_run: list[str] = ["exact_diff_scope"]
    validated_files: list[str] = []
    deleted_files: list[str] = []
    ignored_existing_count = 0
    new_errors: list[dict[str, object]] = []
    liquid_paths: list[str] = []
    head_by_path: dict[str, bytes | None] = {}
    current_by_path: dict[str, bytes | None] = {}

    _assert_repository_root(
        workspace,
        environment=environment,
        deadline=deadline,
    )

    for relative_path in paths:
        baseline_data = _head_file_bytes(
            workspace,
            relative_path,
            environment=environment,
            deadline=deadline,
        )
        current_data = _current_file_bytes(workspace, relative_path)
        head_by_path[relative_path] = baseline_data
        current_by_path[relative_path] = current_data
        validated_files.append(relative_path)

        if current_data is None:
            deleted_files.append(relative_path)
            continue

        if Path(relative_path).suffix.lower() == ".liquid":
            liquid_paths.append(relative_path)
            continue

        check_name, baseline_error = _single_file_validation_error(
            relative_path=relative_path,
            data=baseline_data,
            environment=environment,
            deadline=deadline,
        )
        _, current_error = _single_file_validation_error(
            relative_path=relative_path,
            data=current_data,
            environment=environment,
            deadline=deadline,
        )
        checks_run.append(check_name)
        if current_error is None:
            continue
        detail = {
            "path": relative_path,
            "check": check_name,
            "message": current_error,
        }
        if baseline_error is not None:
            ignored_existing_count += 1
        else:
            new_errors.append(detail)

    theme_check_runs = 0
    if liquid_paths:
        checks_run.append("shopify_theme_check_scoped_differential")
        with tempfile.TemporaryDirectory(
            prefix="bdb-shopify-scoped-check-"
        ) as temporary:
            temporary_root = Path(temporary)
            baseline_root = temporary_root / "baseline"
            current_root = temporary_root / "current"
            baseline_root.mkdir()
            current_root.mkdir()

            baseline_has_files = False
            current_has_files = False
            for relative_path in liquid_paths:
                baseline_data = head_by_path[relative_path]
                current_data = current_by_path[relative_path]
                if baseline_data is not None:
                    _write_snapshot_file(
                        baseline_root,
                        relative_path,
                        baseline_data,
                    )
                    baseline_has_files = True
                if current_data is not None:
                    _write_snapshot_file(
                        current_root,
                        relative_path,
                        current_data,
                    )
                    current_has_files = True

            baseline_document: object = []
            current_document: object = []
            if baseline_has_files:
                baseline_document, _, _ = _run_theme_check_document(
                    command,
                    baseline_root,
                    environment=environment,
                    deadline=deadline,
                )
                theme_check_runs += 1
            if current_has_files:
                current_document, _, _ = _run_theme_check_document(
                    command,
                    current_root,
                    environment=environment,
                    deadline=deadline,
                )
                theme_check_runs += 1

            comparison = _compare_scoped_theme_documents(
                baseline_document,
                current_document,
                baseline_root=baseline_root,
                current_root=current_root,
                allowed_paths=frozenset(liquid_paths),
            )
            ignored_existing_count += int(comparison["ignored_existing_errors"])
            new_errors.extend(list(comparison["new_errors"]))

    failed = bool(new_errors)
    summary = {
        "schema": "bdb-shopify-scoped-check-profile-v1",
        "profile_id": "shopify_theme_check",
        "status": "failed" if failed else "success",
        "validation_mode": "scoped_minimal",
        "validated_files": validated_files,
        "deleted_files": deleted_files,
        "checks_run": sorted(set(checks_run)),
        "theme_check_runs": theme_check_runs,
        "full_theme_check": "skipped",
        "known_backlog": "recorded_separately",
        "ignored_existing_errors": ignored_existing_count,
        "new_errors": new_errors[:_MAX_REPORTED_NEW_ERRORS],
        "new_errors_truncated": len(new_errors) > _MAX_REPORTED_NEW_ERRORS,
    }
    return ProfileRunOutcome(
        "failed" if failed else "success",
        1 if failed else 0,
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        "",
        int((time.monotonic() - started) * 1000),
    )


def run_shopify_theme_check_profile(
    *,
    workspace_path: str | Path,
    command: Sequence[str],
    timeout_seconds: float,
    environment: Mapping[str, str],
    changed_paths: Sequence[str] | None = None,
) -> ProfileRunOutcome:
    started = time.monotonic()
    workspace = Path(workspace_path).resolve(strict=False)
    if changed_paths is not None:
        try:
            return _run_scoped_minimal_profile(
                workspace=workspace,
                command=command,
                timeout_seconds=timeout_seconds,
                environment=environment,
                changed_paths=changed_paths,
                started=started,
            )
        except subprocess.TimeoutExpired:
            return ProfileRunOutcome(
                "timeout",
                None,
                "",
                f"Scoped Shopify validation timed out after {timeout_seconds:g} seconds",
                int((time.monotonic() - started) * 1000),
            )
        except (
            FileNotFoundError,
            OSError,
            UnicodeError,
            ShopifyThemeCheckProfileError,
            tomllib.TOMLDecodeError,
        ) as exc:
            return ProfileRunOutcome(
                "internal_error",
                None,
                "",
                _tail(f"{type(exc).__name__}: {exc}"),
                int((time.monotonic() - started) * 1000),
            )

    deadline = started + float(timeout_seconds)
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
