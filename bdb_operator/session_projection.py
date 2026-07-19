from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import OperatorApiError, OperatorErrorCode


SESSION_HISTORY_SCHEMA = "bdb-session-history-v1"
SESSION_SUMMARY_SCHEMA = "bdb-session-summary-v1"
SESSION_ATTEMPT_SCHEMA = "bdb-session-attempt-v1"
PROMOTION_RECEIPT_SCHEMA = "bdb-workspace-promotion-v1"
WORKSPACE_STATE_SCHEMA = "bdb-workspace-loop-state-v1"
MAX_SESSION_LIMIT = 100
MAX_ATTEMPTS_PER_SESSION = 20
MAX_RESULT_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SessionProjectionReader:
    """Bounded read-only projection of completed and historical BDB sessions."""

    def __init__(
        self,
        *,
        root: Path,
        alias: str,
        journal_path: Path,
        direct_result_dir: Path | None,
        receipt_root: Path | None,
    ) -> None:
        self.root = root
        self.alias = alias
        self.journal_path = journal_path
        self.direct_result_dir = direct_result_dir
        self.receipt_root = receipt_root

    @classmethod
    def from_workspace_root(cls, workspace_root: str | Path) -> "SessionProjectionReader":
        root = Path(workspace_root).expanduser().resolve(strict=False)
        state_path = root / "workspace-loop-state.json"
        state = _read_config_object(state_path, "workspace loop state")
        if state.get("schema") != WORKSPACE_STATE_SCHEMA:
            raise OperatorApiError(
                OperatorErrorCode.WORKSPACE_STATE_INVALID,
                "Workspace loop state schema is unsupported",
                details={"path": str(state_path), "schema": state.get("schema")},
            )
        alias = _required_string(state, "alias", state_path)
        bridge_config_path = Path(_required_string(state, "bridge_config", state_path)).expanduser().resolve(
            strict=False
        )
        config = _read_config_object(bridge_config_path, "bridge config")
        journal_path = Path(_required_string(config, "journal_path", bridge_config_path)).expanduser().resolve(
            strict=False
        )
        runtime_dir = _resolve_runtime_dir(config)
        direct_result_dir = _resolve_direct_result_dir(config, runtime_dir)
        receipt_root = runtime_dir / "promotions" if runtime_dir is not None else None
        return cls(
            root=root,
            alias=alias,
            journal_path=journal_path,
            direct_result_dir=direct_result_dir,
            receipt_root=receipt_root,
        )

    def list_sessions(self, *, limit: int = 20) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SESSION_LIMIT:
            raise OperatorApiError(
                OperatorErrorCode.INVALID_ARGUMENT,
                f"limit must be between 1 and {MAX_SESSION_LIMIT}",
                details={"limit": limit},
            )
        with self._read_only_connection() as connection:
            sessions = connection.execute(
                """
                SELECT s.session_id, s.repository_id, s.base_sha, s.state,
                       s.created_at, s.updated_at,
                       w.workspace_path, w.revision, w.state_hash
                FROM sessions s
                LEFT JOIN workspaces w ON w.session_id = s.session_id
                ORDER BY s.updated_at DESC, s.session_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            documents = [self._session_document(connection, row) for row in sessions]
        return {
            "schema": SESSION_HISTORY_SCHEMA,
            "project_alias": self.alias,
            "generated_at": utc_now_iso(),
            "limit": limit,
            "sessions": documents,
            "read_only": True,
            "repair_relationships_inferred": False,
        }

    def _session_document(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        session_id = str(row["session_id"])
        command_rows = connection.execute(
            """
            SELECT c.command_id, c.sequence, c.state, c.created_at, c.updated_at,
                   p.operation, p.target_path, p.profile_id,
                   r.status AS result_status, r.error_code, r.result_sha256,
                   r.result_json, r.remote_path, r.created_at AS result_created_at
            FROM commands c
            LEFT JOIN operation_plans p ON p.command_id = c.command_id
            LEFT JOIN results r ON r.command_id = c.command_id
            WHERE c.session_id = ?
            ORDER BY c.sequence ASC
            LIMIT ?
            """,
            (session_id, MAX_ATTEMPTS_PER_SESSION + 1),
        ).fetchall()
        truncated = len(command_rows) > MAX_ATTEMPTS_PER_SESSION
        selected = command_rows[:MAX_ATTEMPTS_PER_SESSION]
        attempts = [self._attempt_document(session_id, command) for command in selected]
        return {
            "schema": SESSION_SUMMARY_SCHEMA,
            "session_id": session_id,
            "repository_id": str(row["repository_id"]),
            "base_sha": str(row["base_sha"]),
            "state": str(row["state"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "workspace": {
                "path": row["workspace_path"],
                "revision": row["revision"],
                "state_hash": row["state_hash"],
            },
            "attempt_count": len(selected),
            "attempts_truncated": truncated,
            "attempts": attempts,
            "repair_group_id": None,
            "repair_relationship_inferred": False,
        }

    def _attempt_document(self, session_id: str, row: sqlite3.Row) -> dict[str, Any]:
        sequence = int(row["sequence"])
        warnings: list[str] = []
        journal_result = _decode_result(row["result_json"], warnings)
        expected_relative = _canonical_result_relative(session_id, sequence)
        remote_path = row["remote_path"]
        if remote_path is not None and remote_path != expected_relative:
            warnings.append("Journal result remote_path is not canonical")

        result_path = None
        result_file = {"exists": False, "valid": False, "warning": None}
        if expected_relative is not None and self.direct_result_dir is not None:
            candidate = (self.direct_result_dir / Path(expected_relative)).resolve(strict=False)
            if _contained(candidate, self.direct_result_dir):
                result_path = str(candidate)
                file_result, file_warning = _read_bounded_json(candidate, MAX_RESULT_BYTES, "result")
                result_file = {
                    "exists": candidate.is_file() and not candidate.is_symlink(),
                    "valid": file_result is not None,
                    "warning": file_warning,
                }
                if file_result is not None:
                    file_hash = _sha256(candidate.read_bytes())
                    if row["result_sha256"] is not None and file_hash != row["result_sha256"]:
                        warnings.append("Result file hash differs from Journal")
                    elif journal_result is None:
                        journal_result = file_result
            else:
                warnings.append("Canonical result path escaped declared result root")
        elif expected_relative is None:
            warnings.append("Session ID cannot be represented as a safe result path")

        result_summary = _result_summary(journal_result, row)
        receipt_path = None
        receipt_summary: dict[str, Any] | None = None
        receipt_file = {"exists": False, "valid": False, "warning": None}
        if self.receipt_root is not None and expected_relative is not None:
            candidate = (self.receipt_root / f"{session_id}-{sequence:06d}.json").resolve(strict=False)
            if _contained(candidate, self.receipt_root):
                receipt_path = str(candidate)
                receipt, receipt_warning = _read_bounded_json(candidate, MAX_RECEIPT_BYTES, "receipt")
                receipt_file = {
                    "exists": candidate.is_file() and not candidate.is_symlink(),
                    "valid": receipt is not None,
                    "warning": receipt_warning,
                }
                if receipt is not None:
                    receipt_summary, validation_warning = _receipt_summary(
                        receipt,
                        session_id=session_id,
                        sequence=sequence,
                        result_sha256=row["result_sha256"],
                        changed_files=result_summary.get("changed_files", []),
                    )
                    if validation_warning is not None:
                        receipt_file["valid"] = False
                        receipt_file["warning"] = validation_warning
                        warnings.append(validation_warning)
            else:
                warnings.append("Canonical receipt path escaped declared receipt root")

        return {
            "schema": SESSION_ATTEMPT_SCHEMA,
            "command_id": str(row["command_id"]),
            "sequence": sequence,
            "command_state": str(row["state"]),
            "operation": row["operation"],
            "target_path": row["target_path"],
            "profile_id": row["profile_id"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "result_created_at": row["result_created_at"],
            "result": result_summary,
            "result_path": result_path,
            "result_file": result_file,
            "receipt_path": receipt_path,
            "receipt_file": receipt_file,
            "receipt": receipt_summary,
            "warnings": warnings,
        }

    @contextmanager
    def _read_only_connection(self) -> Iterator[sqlite3.Connection]:
        if not self.journal_path.is_file():
            raise OperatorApiError(
                OperatorErrorCode.JOURNAL_MISSING,
                "BDB Journal is missing",
                details={"path": str(self.journal_path)},
            )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.journal_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=1.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            yield connection
        except OperatorApiError:
            raise
        except sqlite3.Error as error:
            raise OperatorApiError(
                OperatorErrorCode.JOURNAL_UNAVAILABLE,
                "BDB Journal could not be read",
                details={"path": str(self.journal_path), "reason": str(error)},
            ) from error
        finally:
            if connection is not None:
                connection.close()


def _resolve_runtime_dir(config: dict[str, Any]) -> Path | None:
    value = config.get("runtime_dir")
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve(strict=False)
    worktree = config.get("worktree_root")
    if isinstance(worktree, str) and worktree.strip():
        return Path(worktree).expanduser().resolve(strict=False).parent / "bdb_runtime"
    return None


def _resolve_direct_result_dir(config: dict[str, Any], runtime_dir: Path | None) -> Path | None:
    value = config.get("direct_result_dir")
    if isinstance(value, str) and value.strip():
        path = Path(value).expanduser().resolve(strict=False)
    elif runtime_dir is not None:
        path = runtime_dir / "direct_spool" / "results"
    else:
        return None
    if runtime_dir is None or not _contained(path, runtime_dir) or path == runtime_dir:
        raise OperatorApiError(
            OperatorErrorCode.OBSERVABILITY_CONFIG_INVALID,
            "Declared direct result directory is outside runtime_dir",
            details={"path": str(path), "runtime_dir": str(runtime_dir) if runtime_dir else None},
        )
    return path


def _read_config_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        code = (
            OperatorErrorCode.WORKSPACE_STATE_MISSING
            if label == "workspace loop state"
            else OperatorErrorCode.OBSERVABILITY_CONFIG_MISSING
        )
        raise OperatorApiError(code, f"{label.capitalize()} is missing", details={"path": str(path)})
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        code = (
            OperatorErrorCode.WORKSPACE_STATE_INVALID
            if label == "workspace loop state"
            else OperatorErrorCode.OBSERVABILITY_CONFIG_INVALID
        )
        raise OperatorApiError(code, f"{label.capitalize()} is not valid JSON", details={"path": str(path)}) from error
    if not isinstance(value, dict):
        raise OperatorApiError(
            OperatorErrorCode.OBSERVABILITY_CONFIG_INVALID,
            f"{label.capitalize()} must be an object",
            details={"path": str(path)},
        )
    return value


def _required_string(value: dict[str, Any], key: str, path: Path) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise OperatorApiError(
            OperatorErrorCode.OBSERVABILITY_CONFIG_INVALID,
            f"Required string is missing: {key}",
            details={"path": str(path), "key": key},
        )
    return item


def _canonical_result_relative(session_id: str, sequence: int) -> str | None:
    if not _SAFE_SESSION.fullmatch(session_id):
        return None
    return f"sessions/{session_id}/results/{sequence:06d}.json"


def _decode_result(value: Any, warnings: list[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8", errors="replace")) > MAX_RESULT_BYTES:
        warnings.append("Journal result JSON exceeds the projection limit")
        return None
    try:
        parsed = json.loads(value)
    except (UnicodeError, json.JSONDecodeError):
        warnings.append("Journal result JSON is invalid")
        return None
    if not isinstance(parsed, dict):
        warnings.append("Journal result JSON is not an object")
        return None
    return parsed


def _result_summary(document: dict[str, Any] | None, row: sqlite3.Row) -> dict[str, Any]:
    if document is None:
        return {
            "status": row["result_status"],
            "error_code": row["error_code"],
            "exit_code": None,
            "operation": row["operation"],
            "checkpoint_state": None,
            "rollback_performed": None,
            "changed_files": [],
            "result_sha256": row["result_sha256"],
        }
    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    changed = document.get("changed_files")
    if not isinstance(changed, list) or not all(isinstance(path, str) for path in changed):
        changed = []
    return {
        "status": document.get("status", row["result_status"]),
        "error_code": document.get("error_code", row["error_code"]),
        "exit_code": document.get("exit_code") if isinstance(document.get("exit_code"), int) else None,
        "operation": data.get("operation") or row["operation"],
        "checkpoint_state": data.get("checkpoint_state"),
        "rollback_performed": data.get("rollback_performed") if isinstance(data.get("rollback_performed"), bool) else None,
        "changed_files": changed[:200],
        "result_sha256": row["result_sha256"],
    }


def _read_bounded_json(path: Path, limit: int, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, f"{label.capitalize()} path is not a regular file"
    try:
        size = path.stat().st_size
        if size > limit:
            return None, f"{label.capitalize()} exceeds the projection byte limit"
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, f"{label.capitalize()} is not valid JSON"
    if not isinstance(value, dict):
        return None, f"{label.capitalize()} must be an object"
    return value, None


def _receipt_summary(
    value: dict[str, Any],
    *,
    session_id: str,
    sequence: int,
    result_sha256: Any,
    changed_files: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    if value.get("schema") != PROMOTION_RECEIPT_SCHEMA:
        return None, "Receipt schema is unsupported"
    if value.get("session_id") != session_id or value.get("sequence") != sequence:
        return None, "Receipt identity differs from the session attempt"
    if result_sha256 is not None and value.get("result_sha256") != result_sha256:
        return None, "Receipt result hash differs from Journal"
    receipt_changed = value.get("changed_files")
    if not isinstance(receipt_changed, list) or not all(isinstance(path, str) for path in receipt_changed):
        return None, "Receipt changed_files is invalid"
    if changed_files and receipt_changed != changed_files:
        return None, "Receipt changed_files differs from the durable result"
    source_commit = value.get("source_commit")
    parent_commit = value.get("parent_commit")
    if not isinstance(source_commit, str) or not _SHA40.fullmatch(source_commit):
        return None, "Receipt source commit is invalid"
    if not isinstance(parent_commit, str) or not _SHA40.fullmatch(parent_commit):
        return None, "Receipt parent commit is invalid"
    return {
        "status": value.get("status"),
        "source_commit": source_commit.lower(),
        "parent_commit": parent_commit.lower(),
        "changed_files": receipt_changed[:200],
        "promoted_at": value.get("promoted_at"),
        "result_sha256": value.get("result_sha256"),
    }, None


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
