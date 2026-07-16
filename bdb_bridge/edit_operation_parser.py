from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import PurePosixPath
from typing import Any

from .edit_operation_models import (
    EDIT_OPERATION_SCHEMA,
    MAX_STRUCTURAL_CONTENT_BYTES,
    StructuralEditKind,
    StructuralEditSpec,
)
from .models import BridgeErrorCode
from .protocol import BridgeError, validate_repo_relative_path
from .serializers import canonical_json


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_NAMES = frozenset(
    {".env", "id_rsa", "id_ed25519", "credentials.json", "service-account.json"}
)
_SENSITIVE_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
)


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def is_sensitive_edit_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    return (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or name.startswith(".bdb_")
        or pure.suffix.casefold() in _SENSITIVE_SUFFIXES
    )


def _strict_keys(document: dict[str, Any], expected: frozenset[str]) -> None:
    actual = frozenset(document)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"Structural edit keys mismatch; missing={missing} unexpected={unexpected}",
        )


def _string(document: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = document.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{key} must be a string")
    return value


def _path(document: dict[str, Any], key: str) -> str:
    value = validate_repo_relative_path(_string(document, key))
    if is_sensitive_edit_path(value):
        raise BridgeError(
            BridgeErrorCode.POLICY_DENIED,
            f"Structural editing of sensitive or reserved path is denied: {PurePosixPath(value).name}",
        )
    return value


def _sha256(document: dict[str, Any], key: str) -> str:
    value = _string(document, key)
    if _SHA256_RE.fullmatch(value) is None:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{key} must be lowercase sha256:<64 hex>",
        )
    return value


def _content(document: dict[str, Any]) -> tuple[bytes, str]:
    encoded = _string(document, "content_base64", allow_empty=True)
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "content_base64 must be canonical RFC 4648 base64",
        ) from exc
    canonical = base64.b64encode(data).decode("ascii")
    if canonical != encoded:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "content_base64 must use canonical padding",
        )
    if len(data) > MAX_STRUCTURAL_CONTENT_BYTES:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"Structural edit content exceeds {MAX_STRUCTURAL_CONTENT_BYTES} bytes",
        )
    expected = _sha256(document, "content_sha256")
    if sha256_bytes(data) != expected:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "content_sha256 does not match decoded content",
        )
    return data, expected


def _operation_sha256(payload: dict[str, object]) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def parse_structural_edit(document: dict[str, Any]) -> StructuralEditSpec:
    if not isinstance(document, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "Structural edit must be an object")
    schema = _string(document, "schema")
    if schema != EDIT_OPERATION_SCHEMA:
        raise BridgeError(
            BridgeErrorCode.UNSUPPORTED_SCHEMA,
            f"Unsupported structural edit schema: {schema}",
        )
    kind_value = _string(document, "kind")
    try:
        kind = StructuralEditKind(kind_value)
    except ValueError as exc:
        raise BridgeError(
            BridgeErrorCode.UNSUPPORTED_OPERATION,
            f"Unsupported structural edit kind: {kind_value}",
        ) from exc

    source_path: str | None = None
    destination_path: str | None = None
    content: bytes | None = None
    expected_source_sha256: str | None = None
    content_sha256: str | None = None

    if kind is StructuralEditKind.CREATE_FILE:
        _strict_keys(
            document,
            frozenset({"schema", "kind", "path", "content_base64", "content_sha256"}),
        )
        destination_path = _path(document, "path")
        content, content_sha256 = _content(document)
        normalized: dict[str, object] = {
            "content_base64": base64.b64encode(content).decode("ascii"),
            "content_sha256": content_sha256,
            "kind": kind.value,
            "path": destination_path,
            "schema": schema,
        }
    elif kind is StructuralEditKind.DELETE_FILE:
        _strict_keys(
            document,
            frozenset({"schema", "kind", "path", "expected_sha256"}),
        )
        source_path = _path(document, "path")
        expected_source_sha256 = _sha256(document, "expected_sha256")
        normalized = {
            "expected_sha256": expected_source_sha256,
            "kind": kind.value,
            "path": source_path,
            "schema": schema,
        }
    else:
        _strict_keys(
            document,
            frozenset(
                {"schema", "kind", "source_path", "destination_path", "expected_source_sha256"}
            ),
        )
        source_path = _path(document, "source_path")
        destination_path = _path(document, "destination_path")
        if source_path == destination_path:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "source_path and destination_path must differ",
            )
        source_parent = PurePosixPath(source_path).parent
        destination_parent = PurePosixPath(destination_path).parent
        if kind is StructuralEditKind.RENAME_FILE and source_parent != destination_parent:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "rename_file requires source and destination in the same directory",
            )
        if kind is StructuralEditKind.MOVE_FILE and source_parent == destination_parent:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                "move_file requires a different destination directory",
            )
        expected_source_sha256 = _sha256(document, "expected_source_sha256")
        normalized = {
            "destination_path": destination_path,
            "expected_source_sha256": expected_source_sha256,
            "kind": kind.value,
            "schema": schema,
            "source_path": source_path,
        }

    return StructuralEditSpec(
        schema=schema,
        kind=kind,
        source_path=source_path,
        destination_path=destination_path,
        content=content,
        expected_source_sha256=expected_source_sha256,
        content_sha256=content_sha256,
        operation_sha256=_operation_sha256(normalized),
    )


def structural_edit_document(operation: StructuralEditSpec) -> dict[str, str]:
    if not isinstance(operation, StructuralEditSpec):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "operation must be StructuralEditSpec")
    if not isinstance(operation.kind, StructuralEditKind):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "operation.kind must be StructuralEditKind")
    if operation.kind is StructuralEditKind.CREATE_FILE:
        if (
            operation.destination_path is None
            or operation.content is None
            or operation.content_sha256 is None
        ):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "create_file spec is incomplete")
        return {
            "schema": operation.schema,
            "kind": operation.kind.value,
            "path": operation.destination_path,
            "content_base64": base64.b64encode(operation.content).decode("ascii"),
            "content_sha256": operation.content_sha256,
        }
    if operation.kind is StructuralEditKind.DELETE_FILE:
        if operation.source_path is None or operation.expected_source_sha256 is None:
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "delete_file spec is incomplete")
        return {
            "schema": operation.schema,
            "kind": operation.kind.value,
            "path": operation.source_path,
            "expected_sha256": operation.expected_source_sha256,
        }
    if (
        operation.source_path is None
        or operation.destination_path is None
        or operation.expected_source_sha256 is None
    ):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "relocation spec is incomplete")
    return {
        "schema": operation.schema,
        "kind": operation.kind.value,
        "source_path": operation.source_path,
        "destination_path": operation.destination_path,
        "expected_source_sha256": operation.expected_source_sha256,
    }


def validate_structural_edit_spec(operation: StructuralEditSpec) -> None:
    reparsed = parse_structural_edit(structural_edit_document(operation))
    if reparsed != operation:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            "Structural edit spec does not match its canonical representation",
        )
