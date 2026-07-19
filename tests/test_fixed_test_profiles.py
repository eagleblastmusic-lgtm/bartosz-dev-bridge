from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdb_bridge.fixed_test_profile_support import install_fixed_test_profile_support
from bdb_bridge.fixed_test_profiles import (
    ALLOWED_FIXED_TEST_PROFILES,
    PYTEST_PROFILE,
    UNITTEST_PROFILE,
    fixed_profile_arguments,
)
from bdb_bridge.protocol import BridgeError


ROOT = Path(__file__).resolve().parents[1]


def test_fixed_profiles_have_exact_bounded_arguments() -> None:
    assert ALLOWED_FIXED_TEST_PROFILES == {PYTEST_PROFILE, UNITTEST_PROFILE}
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
