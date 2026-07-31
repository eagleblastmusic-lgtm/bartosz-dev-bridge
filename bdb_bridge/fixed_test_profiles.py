from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Mapping

from .models import BridgeErrorCode
from .protocol import BridgeError


PYTEST_PROFILE = "poc_pytest"
UNITTEST_PROFILE = "poc_unittest"
DOTNET_PROFILE = "poc_dotnet"
SHOPIFY_THEME_CHECK_PROFILE = "shopify_theme_check"

_FIXED_PROFILE_ARGUMENTS: dict[str, tuple[str, ...]] = {
    PYTEST_PROFILE: ("-m", "pytest", "-q"),
    UNITTEST_PROFILE: (
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    ),
    DOTNET_PROFILE: (
        "test",
        "--configuration",
        "Release",
        "--nologo",
        "--verbosity",
        "minimal",
    ),
    SHOPIFY_THEME_CHECK_PROFILE: (
        "theme",
        "check",
        "--config",
        "theme-check:recommended",
        "--fail-level",
        "error",
        "--output",
        "json",
        "--no-color",
    ),
}

_FIXED_PROFILE_EXECUTABLES: dict[str, str] = {
    PYTEST_PROFILE: "python",
    UNITTEST_PROFILE: "python",
    DOTNET_PROFILE: "dotnet",
    SHOPIFY_THEME_CHECK_PROFILE: "shopify",
}

ALLOWED_FIXED_TEST_PROFILES = frozenset(_FIXED_PROFILE_ARGUMENTS)


def fixed_profile_arguments(profile_id: str) -> tuple[str, ...]:
    try:
        return _FIXED_PROFILE_ARGUMENTS[profile_id]
    except KeyError as exc:
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            f"Test profile is not locally allowed: {profile_id}",
        ) from exc


def fixed_profile_command(
    profile_id: str,
    *,
    python_executable: str | Path,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    arguments = fixed_profile_arguments(profile_id)
    executable_kind = _FIXED_PROFILE_EXECUTABLES[profile_id]

    if executable_kind == "python":
        executable = Path(python_executable).expanduser().resolve(strict=False)
        if not executable.is_file():
            raise FileNotFoundError("Configured Python executable does not exist")
        return (str(executable), *arguments)

    path_value = (os.environ if environment is None else environment).get("PATH")
    if executable_kind == "dotnet":
        executable = shutil.which("dotnet", path=path_value)
        if executable is None:
            raise FileNotFoundError("dotnet executable was not found on PATH")
        return (str(Path(executable).resolve(strict=False)), *arguments)

    if executable_kind == "shopify":
        executable = next(
            (
                resolved
                for candidate in ("shopify.cmd", "shopify.exe", "shopify")
                if (resolved := shutil.which(candidate, path=path_value)) is not None
            ),
            None,
        )
        if executable is None:
            raise FileNotFoundError("Shopify CLI executable was not found on PATH")
        return (str(Path(executable).resolve(strict=False)), *arguments)

    raise RuntimeError(f"Unsupported fixed profile executable kind: {executable_kind}")
