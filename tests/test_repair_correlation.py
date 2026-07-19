from __future__ import annotations

import json

import pytest

from bdb_bridge.ingestion_validate import parse_manifest_envelope
from bdb_bridge.models import BridgeErrorCode
from bdb_bridge.protocol import BridgeError, manifest_path_for
from bdb_bridge.repair_correlation import (
    REPAIR_CORRELATION_SCHEMA,
    parse_repair_correlation,
)


INITIAL_SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
REPAIR_SESSION = "018f3f66-6cb3-4f66-9f2e-3d7647d1b702"
CORRELATION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b700"


def correlation(role: str, predecessor: str | None) -> dict[str, object]:
    return {
        "schema": REPAIR_CORRELATION_SCHEMA,
        "correlation_id": CORRELATION_ID,
        "role": role,
        "predecessor_session_id": predecessor,
    }


def manifest(session_id: str, repair: dict[str, object] | None) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.1",
        "session_id": session_id,
        "repository_id": "repo-sample",
        "base_sha": "a" * 40,
        "allowed_paths": ["src/app.py"],
        "created_at": "2026-07-19T18:00:00Z",
        "expires_at": "2026-07-19T19:00:00Z",
    }
    if repair is not None:
        value["repair_correlation"] = repair
    return value


def test_initial_and_repair_correlations_are_normalized() -> None:
    initial = parse_repair_correlation(
        correlation("initial", None), session_id=INITIAL_SESSION
    )
    repair = parse_repair_correlation(
        correlation("repair", INITIAL_SESSION), session_id=REPAIR_SESSION
    )

    assert initial is not None
    assert initial.role == "initial"
    assert initial.predecessor_session_id is None
    assert repair is not None
    assert repair.role == "repair"
    assert repair.predecessor_session_id == INITIAL_SESSION
    assert repair.correlation_id == initial.correlation_id


def test_missing_correlation_is_backward_compatible() -> None:
    parsed = parse_repair_correlation(None, session_id=INITIAL_SESSION)
    assert parsed is None


def test_repair_requires_distinct_predecessor() -> None:
    with pytest.raises(BridgeError) as missing:
        parse_repair_correlation(correlation("repair", None), session_id=REPAIR_SESSION)
    assert missing.value.code == BridgeErrorCode.INVALID_PAYLOAD.value

    with pytest.raises(BridgeError) as same:
        parse_repair_correlation(
            correlation("repair", REPAIR_SESSION), session_id=REPAIR_SESSION
        )
    assert same.value.code == BridgeErrorCode.INVALID_PAYLOAD.value


def test_correlation_rejects_unknown_keys_and_roles() -> None:
    unknown = correlation("initial", None)
    unknown["guessed_from_time"] = True
    with pytest.raises(BridgeError) as extra:
        parse_repair_correlation(unknown, session_id=INITIAL_SESSION)
    assert extra.value.code == BridgeErrorCode.INVALID_PAYLOAD.value

    with pytest.raises(BridgeError) as role:
        parse_repair_correlation(correlation("retry", INITIAL_SESSION), session_id=REPAIR_SESSION)
    assert role.value.code == BridgeErrorCode.INVALID_PAYLOAD.value


def test_manifest_ingestion_persists_explicit_correlation() -> None:
    document = manifest(REPAIR_SESSION, correlation("repair", INITIAL_SESSION))
    parsed = parse_manifest_envelope(
        json.dumps(document),
        source_path=manifest_path_for(REPAIR_SESSION),
    )

    assert parsed["repair_correlation"] == correlation("repair", INITIAL_SESSION)


def test_manifest_ingestion_rejects_invalid_correlation() -> None:
    document = manifest(REPAIR_SESSION, correlation("repair", REPAIR_SESSION))
    with pytest.raises(BridgeError) as error:
        parse_manifest_envelope(
            json.dumps(document),
            source_path=manifest_path_for(REPAIR_SESSION),
        )
    assert error.value.code == BridgeErrorCode.INVALID_PAYLOAD.value
