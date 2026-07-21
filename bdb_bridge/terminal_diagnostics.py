from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .models import CommandState
from .multi_file_patch_runtime import MultiFilePatchRuntimeCoordinator
from .protocol import BridgeError, sanitize_diagnostics
from . import runtime_hardening as _runtime_hardening


_TERMINAL_EVENT = "command.terminal_diagnostic"
_INSTALLED = False


def _error_code(error: BridgeError) -> str:
    code = getattr(error, "code", None)
    return str(getattr(code, "value", code) or "policy_denied")


def _read_terminal_event(
    journal_path: str | Path,
    command_id: str,
) -> dict[str, str] | None:
    database = Path(journal_path).expanduser().resolve(strict=False)
    if not database.is_file() or database.is_symlink():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            row = connection.execute(
                """
                SELECT payload_json
                FROM events
                WHERE command_id = ? AND event_type = ?
                ORDER BY event_id DESC
                LIMIT 1
                """,
                (command_id, _TERMINAL_EVENT),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if row is None or not isinstance(row[0], str):
        return None
    try:
        payload = json.loads(row[0])
    except (json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("error_code")
    detail = payload.get("detail")
    if not isinstance(code, str) or not code or not isinstance(detail, str) or not detail:
        return None
    return {"error_code": code, "detail": detail}


def install_terminal_diagnostics() -> None:
    """Preserve pre-mutation terminal details without changing the journal schema."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_ensure_checkpoint = MultiFilePatchRuntimeCoordinator._ensure_checkpoint
    original_terminal_result: Callable[..., dict[str, Any] | None] = (
        _runtime_hardening._terminal_result_from_journal
    )

    def ensure_checkpoint_with_diagnostic(
        self: MultiFilePatchRuntimeCoordinator,
        command: Any,
        document: dict[str, Any],
        workspace: Any,
        executor: Any,
    ) -> None:
        try:
            original_ensure_checkpoint(self, command, document, workspace, executor)
        except BridgeError as error:
            self._bdb_terminal_diagnostic = {
                "error_code": _error_code(error),
                "detail": sanitize_diagnostics(str(error), limit=500)
                or type(error).__name__,
            }
            raise

    def terminal_claimed_with_diagnostic(
        self: MultiFilePatchRuntimeCoordinator,
        command_id: str,
        state: CommandState,
    ) -> None:
        command = self.journal.get_command(command_id)
        if command is None or command.state is not CommandState.CLAIMED:
            return
        diagnostic = getattr(self, "_bdb_terminal_diagnostic", None)
        now = self.journal._now_fn()
        with self.journal._transaction():
            row = self.journal._transition_command_in_transaction(
                command_id,
                CommandState.CLAIMED,
                state,
                now,
            )
            if isinstance(diagnostic, dict):
                code = diagnostic.get("error_code")
                detail = diagnostic.get("detail")
                if isinstance(code, str) and code and isinstance(detail, str) and detail:
                    self.journal._append_event_in_transaction(
                        session_id=row[1],
                        command_id=command_id,
                        event_type=_TERMINAL_EVENT,
                        payload={"error_code": code, "detail": detail},
                        created_at=now,
                    )
        self._bdb_terminal_diagnostic = None

    def terminal_result_with_diagnostic(
        journal_path: str | Path,
        session_id: str,
        sequence: int,
    ) -> dict[str, Any] | None:
        result = original_terminal_result(journal_path, session_id, sequence)
        if result is None:
            return None
        command_id = result.get("command_id")
        if not isinstance(command_id, str):
            return result
        diagnostic = _read_terminal_event(journal_path, command_id)
        if diagnostic is None:
            return result
        detail = diagnostic["detail"]
        data = result.get("data")
        if not isinstance(data, dict):
            data = {}
        result["error_code"] = diagnostic["error_code"]
        result["summary"] = (
            f"Command ended before file mutation: {result.get('status')} — {detail}"
        )
        result["data"] = {
            **data,
            "terminal_error_code": diagnostic["error_code"],
            "terminal_detail": detail,
        }
        return result

    MultiFilePatchRuntimeCoordinator._ensure_checkpoint = ensure_checkpoint_with_diagnostic
    MultiFilePatchRuntimeCoordinator._terminal_claimed = terminal_claimed_with_diagnostic
    _runtime_hardening._terminal_result_from_journal = terminal_result_with_diagnostic
