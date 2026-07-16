from __future__ import annotations

import base64
import binascii
from typing import Any

from .edit_operation_models import (
    EDIT_OPERATION_SCHEMA,
    MAX_STRUCTURAL_CONTENT_BYTES,
    StructuralEditKind,
    StructuralEditSpec,
)
from .edit_operation_parser import (
    is_sensitive_edit_path,
    parse_structural_edit,
    sha256_bytes,
    structural_edit_document,
)
from .models import BridgeErrorCode
from .multi_file_patch_models import (
    FILE_REPLACEMENT_SCHEMA,
    MAX_BATCH_CONTENT_BYTES,
    MAX_BATCH_OPERATIONS,
    MULTI_FILE_PATCH_SCHEMA,
    BatchOperation,
    FileReplacementSpec,
    MultiFilePatchSpec,
)
from .protocol import BridgeError, validate_repo_relative_path
from .serializers import canonical_json


def _strict_keys(document: dict[str, Any], expected: frozenset[str], *, label: str) -> None:
    actual = frozenset(document)
    if actual != expected:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{label} keys mismatch; missing={sorted(expected - actual)} unexpected={sorted(actual - expected)}",
        )


def _string(document: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = document.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{key} must be a string")
    return value


def _digest(document: dict[str, Any], key: str) -> str:
    value = _string(document, key)
    if len(value) != 71 or not value.startswith("sha256:"):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{key} must be sha256:<64 lowercase hex>")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{key} must be sha256:<64 lowercase hex>")
    return value


def _path(document: dict[str, Any], key: str) -> str:
    value = validate_repo_relative_path(_string(document, key))
    if is_sensitive_edit_path(value):
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            "File replacement path is sensitive or reserved",
        )
    return value


def _decode_content(document: dict[str, Any]) -> tuple[bytes, str]:
    encoded = _string(document, "content_base64", allow_empty=True)
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "content_base64 is not canonical base64") from exc
    if base64.b64encode(content).decode("ascii") != encoded:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "content_base64 has noncanonical padding")
    if len(content) > MAX_STRUCTURAL_CONTENT_BYTES:
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            f"Replacement content exceeds {MAX_STRUCTURAL_CONTENT_BYTES} bytes",
        )
    expected = _digest(document, "content_sha256")
    if sha256_bytes(content) != expected:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "content_sha256 does not match content")
    return content, expected


def file_replacement_document(operation: FileReplacementSpec) -> dict[str, str]:
    if not isinstance(operation, FileReplacementSpec):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "operation must be FileReplacementSpec")
    return {
        "schema": operation.schema,
        "kind": operation.kind,
        "path": operation.path,
        "expected_sha256": operation.expected_sha256,
        "content_base64": base64.b64encode(operation.content).decode("ascii"),
        "content_sha256": operation.content_sha256,
    }


def parse_file_replacement(document: dict[str, Any]) -> FileReplacementSpec:
    if not isinstance(document, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "File replacement must be an object")
    _strict_keys(
        document,
        frozenset(
            {"schema", "kind", "path", "expected_sha256", "content_base64", "content_sha256"}
        ),
        label="File replacement",
    )
    if _string(document, "schema") != FILE_REPLACEMENT_SCHEMA:
        raise BridgeError(BridgeErrorCode.UNSUPPORTED_SCHEMA, "Unsupported file replacement schema")
    if _string(document, "kind") != "replace_file":
        raise BridgeError(BridgeErrorCode.UNSUPPORTED_OPERATION, "File replacement kind must be replace_file")
    path = _path(document, "path")
    expected_sha256 = _digest(document, "expected_sha256")
    content, content_sha256 = _decode_content(document)
    normalized = {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_sha256": content_sha256,
        "expected_sha256": expected_sha256,
        "kind": "replace_file",
        "path": path,
        "schema": FILE_REPLACEMENT_SCHEMA,
    }
    return FileReplacementSpec(
        schema=FILE_REPLACEMENT_SCHEMA,
        kind="replace_file",
        path=path,
        expected_sha256=expected_sha256,
        content=content,
        content_sha256=content_sha256,
        operation_sha256=sha256_bytes(canonical_json(normalized).encode("utf-8")),
    )


def operation_document(operation: BatchOperation) -> dict[str, str]:
    if isinstance(operation, StructuralEditSpec):
        return structural_edit_document(operation)
    if isinstance(operation, FileReplacementSpec):
        return file_replacement_document(operation)
    raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Unsupported batch operation object")


def _operation_content_bytes(operation: BatchOperation) -> int:
    if isinstance(operation, FileReplacementSpec):
        return len(operation.content)
    if operation.kind is StructuralEditKind.CREATE_FILE and operation.content is not None:
        return len(operation.content)
    return 0


def multi_file_patch_document(patch: MultiFilePatchSpec) -> dict[str, object]:
    if not isinstance(patch, MultiFilePatchSpec):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "patch must be MultiFilePatchSpec")
    return {
        "schema": patch.schema,
        "operations": [operation_document(operation) for operation in patch.operations],
    }


def parse_multi_file_patch(document: dict[str, Any]) -> MultiFilePatchSpec:
    if not isinstance(document, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Multi-file patch must be an object")
    _strict_keys(document, frozenset({"schema", "operations"}), label="Multi-file patch")
    if _string(document, "schema") != MULTI_FILE_PATCH_SCHEMA:
        raise BridgeError(BridgeErrorCode.UNSUPPORTED_SCHEMA, "Unsupported multi-file patch schema")
    raw_operations = document.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "operations must be a non-empty list")
    if len(raw_operations) > MAX_BATCH_OPERATIONS:
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            f"Multi-file patch exceeds {MAX_BATCH_OPERATIONS} operations",
        )
    operations: list[BatchOperation] = []
    supplied_content_bytes = 0
    for index, raw_operation in enumerate(raw_operations):
        if not isinstance(raw_operation, dict):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"operations[{index}] must be an object")
        schema = raw_operation.get("schema")
        if schema == EDIT_OPERATION_SCHEMA:
            operation = parse_structural_edit(raw_operation)
        elif schema == FILE_REPLACEMENT_SCHEMA:
            operation = parse_file_replacement(raw_operation)
        else:
            raise BridgeError(
                BridgeErrorCode.UNSUPPORTED_SCHEMA,
                f"Unsupported operation schema at index {index}",
            )
        supplied_content_bytes += _operation_content_bytes(operation)
        if supplied_content_bytes > MAX_BATCH_CONTENT_BYTES:
            raise BridgeError(
                BridgeErrorCode.POLICY_DENIED,
                f"Multi-file patch content exceeds {MAX_BATCH_CONTENT_BYTES} bytes",
            )
        operations.append(operation)
    normalized: dict[str, object] = {
        "operations": [operation_document(operation) for operation in operations],
        "schema": MULTI_FILE_PATCH_SCHEMA,
    }
    return MultiFilePatchSpec(
        schema=MULTI_FILE_PATCH_SCHEMA,
        operations=tuple(operations),
        operation_count=len(operations),
        supplied_content_bytes=supplied_content_bytes,
        patch_sha256=sha256_bytes(canonical_json(normalized).encode("utf-8")),
    )


def validate_multi_file_patch_spec(patch: MultiFilePatchSpec) -> None:
    reparsed = parse_multi_file_patch(multi_file_patch_document(patch))
    if reparsed != patch:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Multi-file patch spec does not match its canonical representation",
        )
