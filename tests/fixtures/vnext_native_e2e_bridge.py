from __future__ import annotations

import json
from pathlib import Path
import sys
import uuid

from bdb_vnext.composition import BROWSER_EXTENSION_ID, GENERATION_ID, NATIVE_HOST_NAME, PROTOCOL_GENERATION
from bdb_vnext.m9b_native_host import M9B_NATIVE_RESPONSE_SCHEMA, M9B_NATIVE_REQUEST_SCHEMA, M9bNativeError, VNextNativeConfig, handle_message

runtime_root = Path(sys.argv[1])
message = json.load(sys.stdin)
message.setdefault("schema", M9B_NATIVE_REQUEST_SCHEMA)
message.setdefault("request_id", "e2e-" + uuid.uuid4().hex)
message.setdefault("protocol_generation", PROTOCOL_GENERATION)
message.setdefault("browser_extension_id", BROWSER_EXTENSION_ID)
config = VNextNativeConfig(
    runtime_root=runtime_root,
    legacy_runtime_root=runtime_root.parent / "legacy",
    bootstrap_authority_root=runtime_root.parent / "bootstrap",
)
try:
    response = handle_message(config, message)
except M9bNativeError as exc:
    response = {
        "schema": M9B_NATIVE_RESPONSE_SCHEMA,
        "status": "failed",
        "request_id": message["request_id"],
        "generation_id": GENERATION_ID,
        "protocol_generation": PROTOCOL_GENERATION,
        "native_host_name": NATIVE_HOST_NAME,
        "browser_extension_id": BROWSER_EXTENSION_ID,
        "error_code": exc.code,
        "error": str(exc),
    }
print(json.dumps(response, separators=(",", ":")))
