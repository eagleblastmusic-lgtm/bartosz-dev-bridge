from __future__ import annotations

from .edit_operation_parser import sha256_bytes
from .models import BridgeErrorCode
from .multi_file_patch_models import MAX_TEXT_FILE_BYTES, TextReplacementSpec
from .protocol import BridgeError


def _uniform_line_ending(text: str) -> str | None:
    without_crlf = text.replace("\r\n", "")
    has_crlf = "\r\n" in text
    has_lf = "\n" in without_crlf
    has_cr = "\r" in without_crlf
    kinds = int(has_crlf) + int(has_lf) + int(has_cr)
    if kinds != 1:
        return None
    if has_crlf:
        return "\r\n"
    return "\n" if has_lf else "\r"


def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _bounded_utf8(data: bytes, *, label: str) -> str:
    if len(data) > MAX_TEXT_FILE_BYTES:
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            f"{label} exceeds {MAX_TEXT_FILE_BYTES} bytes",
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{label} must be valid UTF-8",
        ) from exc


def apply_exact_text_replacement(
    source: bytes,
    operation: TextReplacementSpec,
) -> bytes:
    """Apply one parsed text-replacement operation without touching the workspace."""

    if not isinstance(source, bytes):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "source must be bytes")
    if not isinstance(operation, TextReplacementSpec):
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "operation must be TextReplacementSpec",
        )
    if sha256_bytes(source) != operation.expected_sha256:
        raise BridgeError(
            BridgeErrorCode.REPLACE_MISMATCH,
            "Text replacement source hash mismatch",
        )

    current = _bounded_utf8(source, label="Text replacement source")
    target_eol = _uniform_line_ending(current)

    for index, replacement in enumerate(operation.replacements, start=1):
        old_text = replacement.old
        new_text = replacement.new
        if not old_text:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"Replacement {index}: old must be non-empty",
            )

        count = current.count(old_text)
        if count == 1:
            rendered_new = new_text
            if target_eol and ("\n" in new_text or "\r" in new_text):
                rendered_new = _to_lf(new_text).replace("\n", target_eol)
            current = current.replace(old_text, rendered_new, 1)
            continue

        normalized_old = _to_lf(old_text)
        can_compare_eol_agnostic = (
            count == 0
            and target_eol is not None
            and ("\n" in old_text or "\r" in old_text)
        )
        normalized_current = _to_lf(current) if can_compare_eol_agnostic else current
        normalized_count = (
            normalized_current.count(normalized_old)
            if can_compare_eol_agnostic
            else count
        )
        if normalized_count != 1:
            raise BridgeError(
                BridgeErrorCode.REPLACE_MISMATCH,
                f"Replacement {index}: expected exactly one match, found {normalized_count}",
            )

        normalized_new = _to_lf(new_text)
        current = normalized_current.replace(normalized_old, normalized_new, 1)
        current = current.replace("\n", target_eol)

    encoded = current.encode("utf-8")
    if len(encoded) > MAX_TEXT_FILE_BYTES:
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            f"Text replacement result exceeds {MAX_TEXT_FILE_BYTES} bytes",
        )
    return encoded
