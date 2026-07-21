from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
from pathlib import Path
from typing import Any


WINDOWS_GUI_SUBSYSTEM = 2
WINDOWS_CREATE_NO_WINDOW = 0x08000000
MAX_NATIVE_MESSAGE_BYTES = 1024 * 1024
REQUEST_SCHEMA = "bdb-native-request-v1"
RESPONSE_SCHEMA = "bdb-native-response-v1"


def read_pe_subsystem(path: Path) -> int:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise RuntimeError("Native Host executable does not have an MZ header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    optional_header = pe_offset + 24
    if optional_header + 70 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise RuntimeError("Native Host executable does not have a complete PE header")
    magic = struct.unpack_from("<H", data, optional_header)[0]
    if magic not in (0x10B, 0x20B):
        raise RuntimeError(f"Unsupported PE optional-header magic: 0x{magic:04x}")
    return struct.unpack_from("<H", data, optional_header + 68)[0]


def encode_native_message(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_NATIVE_MESSAGE_BYTES:
        raise RuntimeError("Native request is outside the framing limit")
    return struct.pack("<I", len(payload)) + payload


def decode_native_message(frame: bytes) -> dict[str, Any]:
    if len(frame) < 4:
        raise RuntimeError("Native Host returned an incomplete frame header")
    length = struct.unpack_from("<I", frame, 0)[0]
    if not 0 < length <= MAX_NATIVE_MESSAGE_BYTES:
        raise RuntimeError(f"Native Host returned an invalid frame length: {length}")
    if len(frame) != length + 4:
        raise RuntimeError(
            f"Native Host returned {len(frame) - 4} payload bytes; expected {length}"
        )
    try:
        value = json.loads(frame[4:].decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Native Host returned invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Native Host response root is not an object")
    return value


def check_native_host(executable: Path, config: Path, origin: str) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("The windowless Native Host executable check supports Windows only")
    executable = executable.expanduser().resolve(strict=True)
    config = config.expanduser().resolve(strict=True)
    subsystem = read_pe_subsystem(executable)
    if subsystem != WINDOWS_GUI_SUBSYSTEM:
        raise RuntimeError(
            f"Native Host PE subsystem is {subsystem}; expected Windows GUI ({WINDOWS_GUI_SUBSYSTEM})"
        )

    request = {
        "schema": REQUEST_SCHEMA,
        "request_id": "windowless-artifact-check",
        "action": "status",
    }
    completed = subprocess.run(
        [str(executable), origin, "--config", str(config)],
        input=encode_native_message(request),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        shell=False,
        creationflags=WINDOWS_CREATE_NO_WINDOW,
    )
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(
            f"Windowless Native Host exited with code {completed.returncode}{detail}"
        )
    if stderr:
        raise RuntimeError(f"Windowless Native Host wrote unexpected stderr: {stderr}")

    response = decode_native_message(completed.stdout)
    if response.get("schema") != RESPONSE_SCHEMA:
        raise RuntimeError(f"Unexpected Native Host response schema: {response.get('schema')!r}")
    if response.get("request_id") != request["request_id"]:
        raise RuntimeError("Native Host response request_id does not match")
    if response.get("status") != "status":
        raise RuntimeError(f"Native Host status request failed: {response}")
    return {
        "status": "pass",
        "executable": str(executable),
        "config": str(config),
        "origin": origin,
        "pe_subsystem": subsystem,
        "native_protocol": "pass",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a windowless BDB Native Messaging executable on Windows."
    )
    parser.add_argument("--executable", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()

    result = check_native_host(
        Path(args.executable),
        Path(args.config),
        args.origin,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
