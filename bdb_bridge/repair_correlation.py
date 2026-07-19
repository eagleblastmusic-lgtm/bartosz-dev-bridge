from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import BridgeErrorCode
from .protocol import BridgeError, validate_session_id


REPAIR_CORRELATION_SCHEMA = "bdb-repair-correlation-v1"
_REPAIR_ROLES = frozenset({"initial", "repair"})
_ALLOWED_KEYS = frozenset({"schema", "correlation_id", "role", "predecessor_session_id"})


@dataclass(frozen=True)
class RepairCorrelation:
    correlation_id: str
    role: str
    predecessor_session_id: str | None
    schema: str = REPAIR_CORRELATION_SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "correlation_id": self.correlation_id,
            "role": self.role,
            "predecessor_session_id": self.predecessor_session_id,
        }


def parse_repair_correlation(
    value: Any,
    *,
    session_id: str,
    field: str = "repair_correlation",
) -> RepairCorrelation | None:
    """Validate an explicit cross-session repair relationship.

    Missing correlation is valid for backwards compatibility. No relationship is
    ever inferred from timestamps, aliases, filenames, or ordering.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field} must be an object or null")
    unexpected = sorted(set(value) - _ALLOWED_KEYS)
    if unexpected:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{field} contains unsupported keys: {', '.join(unexpected)}",
        )
    if value.get("schema") != REPAIR_CORRELATION_SCHEMA:
        raise BridgeError(
            BridgeErrorCode.UNSUPPORTED_SCHEMA,
            f"{field}.schema must be {REPAIR_CORRELATION_SCHEMA}",
        )
    correlation_id = value.get("correlation_id")
    if not isinstance(correlation_id, str):
        raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, f"{field}.correlation_id must be a string")
    validate_session_id(correlation_id)

    role = value.get("role")
    if not isinstance(role, str) or role not in _REPAIR_ROLES:
        raise BridgeError(
            BridgeErrorCode.INVALID_PAYLOAD,
            f"{field}.role must be one of: initial, repair",
        )

    predecessor = value.get("predecessor_session_id")
    if role == "initial":
        if predecessor is not None:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"{field}.predecessor_session_id must be null for initial role",
            )
        predecessor_id = None
    else:
        if not isinstance(predecessor, str):
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"{field}.predecessor_session_id is required for repair role",
            )
        validate_session_id(predecessor)
        if predecessor == session_id:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"{field}.predecessor_session_id cannot equal the current session_id",
            )
        predecessor_id = predecessor

    return RepairCorrelation(
        correlation_id=correlation_id,
        role=role,
        predecessor_session_id=predecessor_id,
    )
