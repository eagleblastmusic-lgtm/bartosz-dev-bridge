from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge.fixed_test_profile_support import install_fixed_test_profile_support
from bdb_bridge.fixed_test_profiles import (
    ALLOWED_FIXED_TEST_PROFILES,
    DOTNET_PROFILE,
    PYTEST_PROFILE,
    UNITTEST_PROFILE,
    fixed_profile_arguments,
)
from bdb_bridge.protocol import BridgeError


ROOT = Path(__file__).resolve().parents[1]


def test_fixed_profiles_have_exact_bounded_arguments() -> None:
    assert ALLOWED_FIXED_TEST_PROFILES == {
        PYTEST_PROFILE,
        UNITTEST_PROFILE,
        DOTNET_PROFILE,
    }
    assert fixed_profile_arguments(PYTEST_PROFILE) == ("-m", "pytest", "-q")
    assert fixed_profile_arguments(UNITTEST_PROFILE) == (
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    )
    assert fixed_profile_arguments(DOTNET_PROFILE) == (
        "test",
        "--configuration",
        "Release",
        "--nologo",
        "--verbosity",
        "minimal",
    )


def test_unknown_fixed_profile_is_denied() -> None:
    with pytest.raises(BridgeError) as exc:
        fixed_profile_arguments("user-supplied-command")
    assert str(exc.value.code) == "policy_denied"


def test_unittest_profile_runs_standard_library_discovery(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(2 + 3, 5)\n",
        encoding="utf-8",
    )

    class Execution:
        pass

    class Runtime:
        pass

    install_fixed_test_profile_support(Execution, Runtime)
    execution = Execution()
    execution.config = SimpleNamespace(
        python_executable=sys.executable,
        test_timeout_seconds=30,
    )
    outcome = execution._run_profile(SimpleNamespace(path=tmp_path), UNITTEST_PROFILE)

    assert outcome.status == "success"
    assert outcome.exit_code == 0
    assert "Ran 1 test" in outcome.stderr
    assert "OK" in outcome.stderr


def test_dotnet_profile_runs_only_the_fixed_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="3 tests passed", stderr="")

    monkeypatch.setattr(
        "bdb_bridge.fixed_test_profiles.shutil.which",
        lambda *_args, **_kwargs: str(tmp_path / "dotnet.exe"),
    )
    monkeypatch.setattr(
        "bdb_bridge.fixed_test_profile_support.subprocess.run",
        fake_run,
    )

    class Execution:
        pass

    class Runtime:
        pass

    install_fixed_test_profile_support(Execution, Runtime)
    execution = Execution()
    execution.config = SimpleNamespace(
        python_executable=sys.executable,
        test_timeout_seconds=120,
    )
    outcome = execution._run_profile(SimpleNamespace(path=tmp_path), DOTNET_PROFILE)

    assert outcome.status == "success"
    assert outcome.exit_code == 0
    assert captured["command"] == [
        str((tmp_path / "dotnet.exe").resolve()),
        "test",
        "--configuration",
        "Release",
        "--nologo",
        "--verbosity",
        "minimal",
    ]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 120
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["DOTNET_CLI_TELEMETRY_OPTOUT"] == "1"
    assert environment["DOTNET_NOLOGO"] == "1"


def test_dotnet_profile_reports_missing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "bdb_bridge.fixed_test_profiles.shutil.which",
        lambda *_args, **_kwargs: None,
    )

    class Execution:
        pass

    class Runtime:
        pass

    install_fixed_test_profile_support(Execution, Runtime)
    execution = Execution()
    execution.config = SimpleNamespace(
        python_executable=sys.executable,
        test_timeout_seconds=120,
    )
    outcome = execution._run_profile(SimpleNamespace(path=tmp_path), DOTNET_PROFILE)

    assert outcome.status == "internal_error"
    assert outcome.exit_code is None
    assert "dotnet executable was not found on PATH" in outcome.stderr


def test_profile_support_has_no_arbitrary_shell_path() -> None:
    support = (ROOT / "bdb_bridge" / "fixed_test_profile_support.py").read_text(
        encoding="utf-8"
    )
    registry = (ROOT / "bdb_bridge" / "fixed_test_profiles.py").read_text(
        encoding="utf-8"
    )

    assert "shell=False" in support
    assert "shell=True" not in support
    assert "input(" not in support
    assert "user-supplied" not in registry
    assert "_FIXED_PROFILE_ARGUMENTS" in registry
