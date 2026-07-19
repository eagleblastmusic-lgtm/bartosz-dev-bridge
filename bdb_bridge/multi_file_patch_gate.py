from __future__ import annotations

from typing import Any, Callable

from .fixed_test_profiles import ALLOWED_FIXED_TEST_PROFILES, PYTEST_PROFILE
from .models import BridgeErrorCode
from .multi_file_patch_models import MULTI_FILE_PATCH_SCHEMA
from .multi_file_patch_parser import parse_multi_file_patch
from .protocol import BridgeError


MULTI_FILE_PATCH_OPERATION = "multi_file_patch"
MULTI_FILE_PATCH_PROFILE = PYTEST_PROFILE
MULTI_FILE_PATCH_PROFILES = ALLOWED_FIXED_TEST_PROFILES


def _canonical_hash(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{field} must be sha256:<64 lowercase hex>",
        )
    return value


def validate_multi_file_patch_command(document: dict[str, Any]) -> None:
    if document.get("operation") != MULTI_FILE_PATCH_OPERATION:
        return
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "payload must be an object")
    expected_keys = frozenset({"profile_id", "patch"})
    actual_keys = frozenset(payload)
    if actual_keys != expected_keys:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"multi_file_patch payload keys mismatch; missing={sorted(expected_keys - actual_keys)} "
            f"unexpected={sorted(actual_keys - expected_keys)}",
        )
    profile_id = payload.get("profile_id")
    if profile_id not in MULTI_FILE_PATCH_PROFILES:
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            "multi_file_patch profile_id must be one of the fixed local profiles: "
            + ", ".join(sorted(MULTI_FILE_PATCH_PROFILES)),
        )
    patch = payload.get("patch")
    if not isinstance(patch, dict) or patch.get("schema") != MULTI_FILE_PATCH_SCHEMA:
        raise BridgeError(
            BridgeErrorCode.UNSUPPORTED_SCHEMA,
            f"multi_file_patch payload.patch must use {MULTI_FILE_PATCH_SCHEMA}",
        )
    parse_multi_file_patch(patch)
    _canonical_hash(document.get("expected_state_hash"), "expected_state_hash")


def install_multi_file_patch_command_gate() -> None:
    from . import ingestion as ingestion_module
    from . import ingestion_validate as validation_module

    if getattr(validation_module, "_ghb2d_gate_installed", False):
        return
    original: Callable[..., dict[str, Any]] = validation_module.parse_command_envelope

    def guarded(content: str, *, source_path: str) -> dict[str, Any]:
        parsed = original(content, source_path=source_path)
        validate_multi_file_patch_command(parsed)
        return parsed

    validation_module.parse_command_envelope = guarded
    ingestion_module.parse_command_envelope = guarded
    setattr(validation_module, "_ghb2d_gate_installed", True)
