from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .common import (
    BridgeError,
    COMMAND_PATH_RE,
    EXECUTOR_VERSION,
    MAX_TAIL_CHARS,
    SCHEMA_VERSION,
    finalize_result,
    require_int,
    require_string,
    result_path_for,
    sha256_text,
    summary_from_test,
    tail,
    utc_now,
    validate_path_pattern,
    validate_session_id,
)
from .config import BridgeConfig
from .git_ops import ControlRepository
from .workspace import Workspace


class PocBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.control = ControlRepository(config.control_repo_path)
        self.active_session_id: str | None = None
        self.last_sequence = 0
        self.workspace: Workspace | None = None

    def preflight(self) -> None:
        self.control.preflight()
        if not Path(self.config.python_executable).is_file():
            raise BridgeError("invalid_python", "Configured python_executable does not exist")
        if self.config.max_sequence != 3:
            raise BridgeError("invalid_config", "POC-0 must stop after exactly three commands")

    def run(self) -> int:
        self.preflight()
        deadline = time.monotonic() + self.config.max_poll_seconds
        while time.monotonic() < deadline:
            self.control.fetch()
            processed = self._process_available_commands()
            if self.last_sequence >= self.config.max_sequence:
                return 0
            if not processed:
                time.sleep(self.config.poll_interval_seconds)
        return 2

    def _process_available_commands(self) -> bool:
        for command_path in self.control.list_command_paths():
            match = COMMAND_PATH_RE.fullmatch(command_path)
            assert match is not None
            session_id = match.group("session")
            sequence = int(match.group("sequence"))
            result_path = result_path_for(session_id, sequence)
            if self.control.result_exists(result_path):
                continue
            if self.active_session_id is not None and session_id != self.active_session_id:
                continue
            if sequence != self.last_sequence + 1 or sequence > self.config.max_sequence:
                continue
            validate_session_id(session_id)
            self.active_session_id = session_id
            self._process_one(command_path, session_id, sequence, result_path)
            self.last_sequence = sequence
            return True
        return False

    def _process_one(self, command_path: str, session_id: str, sequence: int, result_path: str) -> None:
        started_at = utc_now()
        command_ref_sha = self.control.ref_sha("origin/commands")
        revision_before = self.workspace.revision if self.workspace else 0
        state_before = self.workspace.state_hash() if self.workspace else None
        base_result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "command_id": f"{session_id}:{sequence:06d}",
            "sequence": sequence,
            "started_at": started_at,
            "executor_version": EXECUTOR_VERSION,
            "command_commit_sha": command_ref_sha,
            "workspace_revision_before": revision_before,
            "state_hash_before": state_before,
            "changed_files": [],
            "artifacts": [],
            "truncated": False,
        }
        operation_started = time.monotonic()
        try:
            manifest = self.control.read_json("origin/commands", f"sessions/{session_id}/manifest.json")
            command = self.control.read_json("origin/commands", command_path)
            self._validate_manifest(manifest, session_id)
            self._validate_command(command, session_id, sequence)
            if self.workspace is None:
                self.workspace = Workspace(
                    self.config,
                    session_id,
                    require_string(manifest, "base_sha"),
                    list(manifest["allowed_paths"]),
                )
                self.workspace.create()
                revision_before = self.workspace.revision
                state_before = self.workspace.state_hash()
                base_result["workspace_revision_before"] = revision_before
                base_result["state_hash_before"] = state_before

            expected_revision = command.get("expected_revision")
            if expected_revision != self.workspace.revision:
                raise BridgeError(
                    "stale_revision",
                    f"Expected revision {expected_revision}, current revision is {self.workspace.revision}",
                )
            expected_state = command.get("expected_state_hash")
            current_state = self.workspace.state_hash()
            if expected_state is not None and expected_state != current_state:
                raise BridgeError("state_mismatch", "expected_state_hash does not match workspace")

            operation = command["operation"]
            payload = command["payload"]
            if operation == "open_read":
                data = self.workspace.read_range(
                    require_string(payload, "path"),
                    require_int(payload, "start_line"),
                    require_int(payload, "end_line"),
                )
                base_result.update(
                    status="success",
                    exit_code=0,
                    summary=f"Read {data['path']} lines {data['start_line']}-{data['end_line']}",
                    data=data,
                )
            elif operation == "replace_exact_and_test":
                outcome = self.workspace.replace_exact_and_test(
                    payload,
                    self.config.python_executable,
                    self.config.test_timeout_seconds,
                )
                base_result.update(
                    status=outcome["status"],
                    exit_code=outcome["exit_code"],
                    summary=summary_from_test(outcome),
                    stdout_tail=tail(outcome["stdout"]),
                    stderr_tail=tail(outcome["stderr"]),
                    stdout_sha256=sha256_text(outcome["stdout"]),
                    stderr_sha256=sha256_text(outcome["stderr"]),
                    changed_files=outcome["changed_files"],
                    diff=tail(outcome["diff"], 6_000),
                    diff_sha256=sha256_text(outcome["diff"]),
                    truncated=(
                        len(outcome["stdout"]) > MAX_TAIL_CHARS
                        or len(outcome["stderr"]) > MAX_TAIL_CHARS
                        or len(outcome["diff"]) > 6_000
                    ),
                )
            else:
                raise BridgeError("unsupported_operation", f"Unsupported operation: {operation}")
        except BridgeError as exc:
            base_result.update(status=exc.code, exit_code=None, summary=str(exc))
        except Exception as exc:
            base_result.update(status="internal_error", exit_code=None, summary=f"{type(exc).__name__}: {exc}")

        revision_after = self.workspace.revision if self.workspace else revision_before
        state_after = self.workspace.state_hash() if self.workspace else state_before
        base_result.update(
            finished_at=utc_now(),
            duration_ms=int((time.monotonic() - operation_started) * 1000),
            workspace_revision_after=revision_after,
            state_hash_after=state_after,
        )
        self.control.publish_result(result_path, finalize_result(base_result))

    def _validate_manifest(self, manifest: dict[str, Any], session_id: str) -> None:
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise BridgeError("unsupported_schema", "Manifest schema_version must be 1.1")
        if manifest.get("session_id") != session_id:
            raise BridgeError("session_mismatch", "Manifest session_id does not match path")
        validate_session_id(session_id)
        if manifest.get("repository_id") != self.config.repository_id:
            raise BridgeError("policy_denied", "repository_id is not allowed by local policy")
        base_sha = require_string(manifest, "base_sha")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
            raise BridgeError("invalid_base_sha", "base_sha must be an exact 40-character SHA")
        allowed = manifest.get("allowed_paths")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(v, str) for v in allowed):
            raise BridgeError("invalid_manifest", "allowed_paths must be a non-empty string list")
        for pattern in allowed:
            validate_path_pattern(pattern)

    def _validate_command(self, command: dict[str, Any], session_id: str, sequence: int) -> None:
        if command.get("schema_version") != SCHEMA_VERSION:
            raise BridgeError("unsupported_schema", "Command schema_version must be 1.1")
        if command.get("session_id") != session_id:
            raise BridgeError("session_mismatch", "Command session_id does not match path")
        if command.get("sequence") != sequence:
            raise BridgeError("sequence_mismatch", "Command sequence does not match filename")
        if command.get("command_id") != f"{session_id}:{sequence:06d}":
            raise BridgeError("command_id_mismatch", "command_id does not match session and sequence")
        if command.get("operation") not in {"open_read", "replace_exact_and_test"}:
            raise BridgeError("unsupported_operation", "Operation is not allowed in POC-0")
        if not isinstance(command.get("expected_revision"), int) or command["expected_revision"] < 0:
            raise BridgeError("invalid_revision", "expected_revision must be a non-negative integer")
        if not isinstance(command.get("payload"), dict):
            raise BridgeError("invalid_payload", "payload must be an object")
