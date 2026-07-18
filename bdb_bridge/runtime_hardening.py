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
        data = reader.read_blob(entry.object_sha)
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            canonical_files.append(raw)
            continue
        canonical_files.append(
            {
                **raw,
                "bytes": len(data),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
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
) -> dict[str, Any] | None:
    database = Path(journal_path).expanduser().resolve(strict=False)
    if not database.is_file() or database.is_symlink():
        return None

    try:
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

    def hardened_context_build(self: WorkspaceContextBuilder) -> dict[str, Any]:
        return _canonicalize_clean_snapshot(self, original_context_build(self))

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
        while True:
            content = sink.read(remote_path)
            if content is not None:
                return _parse_result_bytes(content)

            terminal = _terminal_result_from_journal(
                repository.bridge_config.journal_path,
                session_id,
                sequence,
            )
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

    NativeHostService._wait_for_result = hardened_wait_for_result  # type: ignore[method-assign]
