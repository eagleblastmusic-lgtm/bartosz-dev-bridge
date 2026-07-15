from __future__ import annotations

import hashlib
import json
from typing import Any

from .protocol import BridgeError

MAX_RESULT_BYTES = 16 * 1024
MAX_TAIL_CHARS = 5_000


def tail(value: str, limit: int = MAX_TAIL_CHARS) -> str:
    return value[-limit:]


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def finalize_result(result: dict[str, Any]) -> str:
    candidate = dict(result)
    was_truncated = bool(candidate.get("truncated", False))
    for field in ("stdout_tail", "stderr_tail", "diff"):
        if field in candidate:
            original = str(candidate[field])
            if len(original) > MAX_TAIL_CHARS:
                was_truncated = True
            candidate[field] = tail(original, MAX_TAIL_CHARS)
    if isinstance(candidate.get("data"), dict) and "content" in candidate["data"]:
        candidate["data"] = dict(candidate["data"])
        original_content = str(candidate["data"]["content"])
        if len(original_content) > 8_000:
            was_truncated = True
        candidate["data"]["content"] = tail(original_content, 8_000)
    candidate["truncated"] = was_truncated

    while True:
        without_marker = dict(candidate)
        without_marker.pop("end_marker", None)
        digest = hashlib.sha256(canonical_json(without_marker).encode("utf-8")).hexdigest()
        candidate["end_marker"] = f"BDB-END:sha256:{digest}"
        serialized = json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2)
        if len(serialized.encode("utf-8")) <= MAX_RESULT_BYTES:
            return serialized
        candidate["truncated"] = True
        shrunk = False
        for field in ("diff", "stdout_tail", "stderr_tail"):
            if len(str(candidate.get(field, ""))) > 1_000:
                candidate[field] = tail(str(candidate[field]), max(1_000, len(str(candidate[field])) // 2))
                shrunk = True
        data = candidate.get("data")
        if isinstance(data, dict) and len(str(data.get("content", ""))) > 1_000:
            data["content"] = tail(str(data["content"]), max(1_000, len(str(data["content"])) // 2))
            shrunk = True
        if not shrunk:
            raise BridgeError("result_too_large", "Unable to fit result into 16 KiB")
