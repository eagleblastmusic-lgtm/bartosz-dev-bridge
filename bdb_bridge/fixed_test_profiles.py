from __future__ import annotations

from .models import BridgeErrorCode
from .protocol import BridgeError


PYTEST_PROFILE = "poc_pytest"
UNITTEST_PROFILE = "poc_unittest"

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
