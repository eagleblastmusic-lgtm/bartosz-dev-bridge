from __future__ import annotations

import hashlib

import pytest

from bdb_bridge.exact_text_edit import apply_exact_text_replacement
from bdb_bridge.multi_file_patch_models import (
    ExactTextReplacement,
    TextReplacementSpec,
)
from bdb_bridge.protocol import BridgeError


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _operation(
    source: bytes,
    *pairs: tuple[str, str],
) -> TextReplacementSpec:
    replacements = tuple(
        ExactTextReplacement(old=old, new=new)
        for old, new in pairs
    )
    return TextReplacementSpec(
        schema="bdb-text-replacement-v1",
        kind="replace_exact_text",
        path="pkg/module.py",
        expected_sha256=_sha256(source),
        replacements=replacements,
        replacement_count=len(replacements),
        supplied_text_bytes=sum(
            len(old.encode("utf-8")) + len(new.encode("utf-8"))
            for old, new in pairs
        ),
        operation_sha256="sha256:" + ("0" * 64),
    )


def test_applies_sequential_exact_replacements() -> None:
    source = b"alpha = 1\nbeta = 2\n"

    result = apply_exact_text_replacement(
        source,
        _operation(
            source,
            ("alpha = 1", "alpha = 10"),
            ("beta = 2\n", ""),
        ),
    )

    assert result == b"alpha = 10\n"


@pytest.mark.parametrize(
    ("source", "old", "found"),
    [
        (b"alpha\n", "missing", 0),
        (b"alpha alpha\n", "alpha", 2),
    ],
)
def test_requires_exactly_one_match(
    source: bytes,
    old: str,
    found: int,
) -> None:
    with pytest.raises(
        BridgeError,
        match=rf"expected exactly one match, found {found}",
    ):
        apply_exact_text_replacement(
            source,
            _operation(source, (old, "replacement")),
        )


def test_accepts_lf_pattern_and_preserves_crlf_file_endings() -> None:
    source = b"first\r\nsecond\r\n"

    result = apply_exact_text_replacement(
        source,
        _operation(
            source,
            ("first\nsecond\n", "first\nchanged\n"),
        ),
    )

    assert result == b"first\r\nchanged\r\n"


def test_rejects_source_hash_mismatch() -> None:
    source = b"alpha\n"
    operation = _operation(source, ("alpha", "beta"))
    changed_source = b"gamma\n"

    with pytest.raises(BridgeError, match="source hash mismatch"):
        apply_exact_text_replacement(changed_source, operation)


def test_rejects_non_utf8_source() -> None:
    source = b"\xff\xfe"
    operation = _operation(source, ("old", "new"))

    with pytest.raises(BridgeError, match="must be valid UTF-8"):
        apply_exact_text_replacement(source, operation)
