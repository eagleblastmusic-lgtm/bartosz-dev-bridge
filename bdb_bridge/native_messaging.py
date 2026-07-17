from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

from .protocol import BridgeError
from .serializers import canonical_json


_HEADER = struct.Struct("=I")
DEFAULT_MAX_MESSAGE_BYTES = 1024 * 1024


def read_native_message(
    stream: BinaryIO,
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> dict[str, Any] | None:
    header = stream.read(_HEADER.size)
    if header == b"":
        return None
    if len(header) != _HEADER.size:
        raise BridgeError("invalid_payload", "Native message header is truncated")
    (size,) = _HEADER.unpack(header)
    if size <= 0 or size > max_message_bytes:
        raise BridgeError("invalid_payload", "Native message length is outside the allowed range")
    payload = stream.read(size)
    if len(payload) != size:
        raise BridgeError("invalid_payload", "Native message payload is truncated")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("invalid_payload", f"Native message must be strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BridgeError("invalid_payload", "Native message root must be an object")
    return value


def encode_native_message(
    value: dict[str, Any],
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> bytes:
    if not isinstance(value, dict):
        raise BridgeError("invalid_payload", "Native response root must be an object")
    payload = canonical_json(value).encode("utf-8", errors="strict")
    if len(payload) <= 0 or len(payload) > max_message_bytes:
        raise BridgeError("result_too_large", "Native response exceeds the allowed size")
    return _HEADER.pack(len(payload)) + payload


def write_native_message(
    stream: BinaryIO,
    value: dict[str, Any],
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> None:
    stream.write(encode_native_message(value, max_message_bytes=max_message_bytes))
    stream.flush()
