from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .git_object_reader import GitObjectReader
from .local_result_sink import LocalResultSink
from .native_host import NativeHostService
from .protocol import BridgeError, result_path_for
from .serializers import finalize_result
from .workspace_context import WorkspaceContextBuilder
from .workspace_manager import Git


_TERMINAL_COMMAND_STATES = frozenset(
    {
        "manual_reconciliation_required",
        "policy_denied",
        "stale_revision",
        "state_mismatch",
        "rejected",
        "expired",
        "cancelled",
    }
)
_EMPTY_SHA256 = "sha256:" + hashlib.sha256(b"").hexdigest()
_INSTALLED = False


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _harden_worktree_add_args(args: Iterable[str]) -> list[str]:
    values = list(args)
    if len(values) >= 2 and values[0] == "worktree" and values[1] == "add":
        return [
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            *values,
        ]
    return values


def _canonicalize_promotion_hashes(
    promotion: object,
    *,
    commit_sha: str,
    entries: dict[str, Any],
    blobs: dict[str, bytes],
) -> object:
    if not isinstance(promotion, dict):
        return promotion
    if promotion.get("source_commit") != commit_sha:
        return promotion
    raw_hashes = promotion.get("file_sha256")
    if not isinstance(raw_hashes, dict):
        return promotion

    canonical_hashes: dict[str, object] = {}
    for raw_path, raw_hash in raw_hashes.items():
        if not isinstance(raw_path, str):
            continue
        entry = entries.get(raw_path)
        if entry is None:
            canonical_hashes[raw_path] = raw_hash
            continue
        data = blobs.get(entry.object_sha)
        canonical_hashes[raw_path] = _sha256(data) if data is not None else raw_hash

    return {
        **promotion,
        "file_sha256": canonical_hashes,
    }


def _canonicalize_clean_snapshot(
    builder: WorkspaceContextBuilder,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    if snapshot.get("source_clean") is not True:
        return snapshot

    reader = GitObjectReader(builder.root)
    commit_sha = reader.resolve_commit("HEAD")
    entries = {
        entry.path: entry
        for entry in reader.list_tree(commit_sha)
        if entry.object_type == "blob" and entry.mode not in {"120000", "160000"}
    }
    requested_shas: list[str] = []
    for raw in snapshot.get("snapshot_files", []):
        if isinstance(raw, dict) and isinstance(raw.get("path"), str):
            entry = entries.get(raw["path"])
            if entry is not None:
                requested_shas.append(entry.object_sha)
    promotion = snapshot.get("latest_promotion")
    if isinstance(promotion, dict) and promotion.get("source_commit") == commit_sha:
        hashes = promotion.get("file_sha256")
        if isinstance(hashes, dict):
            for relative in hashes:
                entry = entries.get(relative) if isinstance(relative, str) else None
                if entry is not None:
                    requested_shas.append(entry.object_sha)
    blobs = reader.read_blobs(tuple(dict.fromkeys(requested_shas)))

    canonical_files: list[dict[str, Any]] = []
    for raw in snapshot.get("snapshot_files", []):
        if not isinstance(raw, dict):
            canonical_files.append(raw)
            continue
        relative = raw.get("path")
        entry = entries.get(relative) if isinstance(relative, str) else None
        if entry is None:
            canonical_files.append(raw)
            continue
        data = blobs.get(entry.object_sha)
        if data is None:
            canonical_files.append(raw)
            continue
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            canonical_files.append(raw)
            continue
        canonical_files.append(
            {
                **raw,
                "bytes": len(data),
                "sha256": _sha256(data),
                "content": text,
            }
        )

    result = {
        **snapshot,
        "snapshot_files": canonical_files,
        "snapshot_bytes": sum(
            int(item.get("bytes", 0))
            for item in canonical_files
            if isinstance(item, dict)
        ),
        "snapshot_source": "git_blobs",
        "latest_promotion": _canonicalize_promotion_hashes(
            snapshot.get("latest_promotion"),
            commit_sha=commit_sha,
            entries=entries,
            blobs=blobs,
        ),
    }
    capabilities = result.get("capabilities")
    if isinstance(capabilities, dict):
        result["capabilities"] = {
            **capabilities,
            "canonical_git_blob_hashes": True,
        }
    return result


def _terminal_result_from_journal(
    journal_path: str | Path,
    session_id: str,
    sequence: int,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    database = Path(journal_path).expanduser().resolve(strict=False)
    if not database.is_file() or database.is_symlink():
        return None

    owns_connection = connection is None
    try:
        if connection is None:
            connection = sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro",
                uri=True,
                timeout=1.0,
            )
            connection.row_factory = sqlite3.Row
        try:
            command = connection.execute(
                """
                SELECT command_id, session_id, sequence, state, command_commit_sha,
                       expected_revision, expected_state_hash, command_json,
                       created_at, updated_at
                FROM commands
                WHERE session_id = ? AND sequence = ?
                LIMIT 1
                """,
                (session_id, sequence),
            ).fetchone()
            if command is None or str(command["state"]) not in _TERMINAL_COMMAND_STATES:
                return None
            workspace = connection.execute(
                """
                SELECT revision, state_hash
                FROM workspaces
                WHERE session_id = ?
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        finally:
            if owns_connection:
                connection.close()
    except sqlite3.Error:
        return None

    state = str(command["state"])
    revision = int(workspace["revision"]) if workspace is not None else int(command["expected_revision"] or 0)
    state_hash = (
        str(workspace["state_hash"])
        if workspace is not None and workspace["state_hash"] is not None
        else str(command["expected_state_hash"] or "")
    )
    operation = None
    try:
        document = json.loads(str(command["command_json"]))
        if isinstance(document, dict) and isinstance(document.get("operation"), str):
            operation = document["operation"]
    except (json.JSONDecodeError, UnicodeError, TypeError):
        operation = None

    return {
        "schema_version": "1.1",
        "session_id": str(command["session_id"]),
        "command_id": str(command["command_id"]),
        "sequence": int(command["sequence"]),
        "started_at": str(command["created_at"]),
        "finished_at": str(command["updated_at"]),
        "duration_ms": 0,
        "executor_version": "0.6.1-terminal",
        "command_commit_sha": command["command_commit_sha"],
        "workspace_revision_before": revision,
        "workspace_revision_after": revision,
        "state_hash_before": state_hash,
        "state_hash_after": state_hash,
        "status": state,
        "error_code": state,
        "exit_code": None,
        "summary": f"Command ended before file mutation: {state}",
        "stdout_tail": "",
        "stderr_tail": "",
        "stdout_sha256": _EMPTY_SHA256,
        "stderr_sha256": _EMPTY_SHA256,
        "changed_files": [],
        "diff": "",
        "diff_sha256": _EMPTY_SHA256,
        "artifacts": [],
        "truncated": False,
        "data": {
            "operation": operation,
            "terminal": "needs_user",
            "terminal_state": state,
            "rollback_performed": False,
        },
    }


def _journal_change_token(journal_path: str | Path) -> tuple[int, int, int, int] | None:
    database = Path(journal_path).expanduser().resolve(strict=False)
    try:
        database_stat = database.stat()
    except OSError:
        return None
    wal = Path(str(database) + "-wal")
    try:
        wal_stat = wal.stat()
        return (
            database_stat.st_mtime_ns,
            database_stat.st_size,
            wal_stat.st_mtime_ns,
            wal_stat.st_size,
        )
    except OSError:
        return (database_stat.st_mtime_ns, database_stat.st_size, 0, 0)


def _parse_result_bytes(content: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("journal_corrupt", "Local result is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise BridgeError("journal_corrupt", "Local result root must be an object")
    return parsed


def install_runtime_hardening() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_git_run = Git.run

    def hardened_git_run(self: Git, args: Iterable[str], **kwargs: Any):
        return original_git_run(self, _harden_worktree_add_args(args), **kwargs)

    Git.run = hardened_git_run  # type: ignore[method-assign]

    original_context_build = WorkspaceContextBuilder.build

    def hardened_context_build(self: WorkspaceContextBuilder, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return _canonicalize_clean_snapshot(self, original_context_build(self, *args, **kwargs))

    WorkspaceContextBuilder.build = hardened_context_build  # type: ignore[method-assign]

    def hardened_wait_for_result(
        self: NativeHostService,
        repository: Any,
        session_id: str,
        sequence: int,
        wait_seconds: float,
    ) -> dict[str, Any] | None:
        remote_path = result_path_for(session_id, sequence)
        sink = LocalResultSink(repository.bridge_config.direct_result_dir)
        deadline = self.monotonic() + wait_seconds
        journal_connection: sqlite3.Connection | None = None
        last_token: tuple[int, int, int, int] | None = None
        try:
            while True:
                content = sink.read(remote_path)
                if content is not None:
                    return _parse_result_bytes(content)

                token = _journal_change_token(repository.bridge_config.journal_path)
                terminal = None
                if token is not None and (journal_connection is None or token != last_token):
                    if journal_connection is None:
                        database = Path(repository.bridge_config.journal_path).expanduser().resolve(strict=False)
                        try:
                            journal_connection = sqlite3.connect(
                                f"file:{database.as_posix()}?mode=ro",
                                uri=True,
                                timeout=1.0,
                            )
                            journal_connection.row_factory = sqlite3.Row
                        except sqlite3.Error:
                            journal_connection = None
                    if journal_connection is not None:
                        terminal = _terminal_result_from_journal(
                            repository.bridge_config.journal_path,
                            session_id,
                            sequence,
                            connection=journal_connection,
                        )
                    last_token = token
                if terminal is not None:
                    payload = finalize_result(terminal).encode("utf-8", errors="strict")
                    try:
                        sink.publish(remote_path, payload)
                    except BridgeError:
                        existing = sink.read(remote_path)
                        if existing is None:
                            raise
                        return _parse_result_bytes(existing)
                    return _parse_result_bytes(payload)

                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    return None
                self.sleeper(min(0.05, remaining))
        finally:
            if journal_connection is not None:
                journal_connection.close()

    NativeHostService._wait_for_result = hardened_wait_for_result  # type: ignore[method-assign]
