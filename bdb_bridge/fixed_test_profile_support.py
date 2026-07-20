from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Type

from .execution import sanitized_test_environment
from .fixed_test_profiles import (
    DOTNET_PROFILE,
    PYTEST_PROFILE,
    fixed_profile_arguments,
    fixed_profile_command,
)
from .models import BridgeErrorCode, ProfileRunOutcome
from .multi_file_patch_recovery_models import MultiFileCheckpointState
from .protocol import BridgeError


_DOTNET_ENVIRONMENT_KEYS = (
    "APPDATA",
    "DOTNET_ROOT",
    "DOTNET_ROOT_X64",
    "HOME",
    "LOCALAPPDATA",
    "NUGET_PACKAGES",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "USERPROFILE",
)


def _profile_environment(profile_id: str) -> dict[str, str]:
    environment = sanitized_test_environment()
    if profile_id == DOTNET_PROFILE:
        for key in _DOTNET_ENVIRONMENT_KEYS:
            value = os.environ.get(key)
            if value:
                environment[key] = value
        environment.update(
            {
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_NOLOGO": "1",
                "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
            }
        )
    return environment


def install_fixed_test_profile_support(
    execution_cls: Type[object],
    runtime_cls: Type[object],
) -> None:
    if getattr(runtime_cls, "_fixed_test_profiles_installed", False):
        return

    def run_profile(
        self: Any,
        workspace: Any,
        profile_id: str = PYTEST_PROFILE,
    ) -> ProfileRunOutcome:
        try:
            command = fixed_profile_command(
                profile_id,
                python_executable=self.config.python_executable,
                environment=os.environ,
            )
        except BridgeError:
            return ProfileRunOutcome(
                "internal_error",
                None,
                "",
                "Profile is not locally allowed",
                0,
            )
        except FileNotFoundError as exc:
            return ProfileRunOutcome(
                "internal_error",
                None,
                "",
                str(exc),
                0,
            )

        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(command),
                cwd=workspace.path,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=self.config.test_timeout_seconds,
                env=_profile_environment(profile_id),
                shell=False,
            )
            status = "success" if completed.returncode == 0 else "failed"
            return ProfileRunOutcome(
                status,
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

    def ensure_profile(self: Any, command_id: str, workspace: Any) -> Any:
        existing = self.journal.get_multi_file_patch_profile_run(command_id)
        if existing is not None:
            return existing
        checkpoint = self.journal.get_multi_file_patch_checkpoint(command_id)
        if checkpoint is None or checkpoint.state is not MultiFileCheckpointState.APPLIED:
            raise BridgeError(
                BridgeErrorCode.INVALID_STATE_TRANSITION,
                "Profile can run only after a complete batch apply",
            )
        command = self.journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Command not found")
        try:
            document = json.loads(command.command_json)
            payload = document["payload"]
            profile_id = payload["profile_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BridgeError(
                BridgeErrorCode.JOURNAL_CORRUPT,
                "Persisted command has no fixed profile identity",
            ) from exc
        fixed_profile_arguments(profile_id)
        started_at = self.journal._now_fn()
        self._fault("BEFORE_GHB2D_PROFILE")
        outcome = self.profile_runner(workspace, profile_id)
        finished_at = self.journal._now_fn()
        profile = self.journal.record_multi_file_patch_profile_run(
            command_id=command_id,
            profile_id=profile_id,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._fault("AFTER_GHB2D_PROFILE_RECORDED")
        return profile

    execution_cls._run_profile = run_profile
    runtime_cls._ensure_profile = ensure_profile
    setattr(runtime_cls, "_fixed_test_profiles_installed", True)
