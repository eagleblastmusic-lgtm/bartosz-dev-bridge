from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .config import BridgeConfig
from .local_result_sink import LocalResultSink
from .local_spool_transport import LOCAL_ENVELOPE_SCHEMA, LocalSpoolWriter
from .local_wake import signal_running_bridge
from .native_messaging import DEFAULT_MAX_MESSAGE_BYTES, read_native_message, write_native_message
from .protocol import (
    BridgeError,
    command_id_for,
    parse_strict_utc_timestamp,
    require_int,
    require_string,
    result_path_for,
    validate_session_id,
)


NATIVE_HOST_NAME = "com.bartosz.dev_bridge"
NATIVE_CONFIG_SCHEMA = "bdb-native-host-config-v1"
NATIVE_ARM_SCHEMA = "bdb-native-arm-v1"
NATIVE_REQUEST_SCHEMA = "bdb-native-request-v1"
NATIVE_RESPONSE_SCHEMA = "bdb-native-response-v1"
_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_WAIT_SECONDS = 120.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def default_native_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data)
    else:
        root = Path.home() / "AppData" / "Local"
    return (root / "BartoszDevBridge" / "native-host.json").resolve(strict=False)


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class NativeHostConfig:
    bridge_config_path: Path
    allowed_origins: tuple[str, ...]
    state_path: Path
    max_wait_seconds: float = 30.0
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES

    @classmethod
    def from_json(cls, path: str | Path) -> "NativeHostConfig":
        config_path = Path(path).expanduser().resolve(strict=True)
        if not config_path.is_file() or config_path.is_symlink():
            raise BridgeError("invalid_config", "Native host config must be a regular file")
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("schema") != NATIVE_CONFIG_SCHEMA:
            raise BridgeError("unsupported_schema", "Native host config schema is unsupported")

        bridge_config_path = Path(require_string(raw, "bridge_config_path")).expanduser().resolve(strict=True)
        if not bridge_config_path.is_file() or bridge_config_path.is_symlink():
            raise BridgeError("invalid_config", "bridge_config_path must identify a regular file")

        origins = raw.get("allowed_origins")
        if (
            not isinstance(origins, list)
            or not origins
            or not all(isinstance(item, str) and _ORIGIN_RE.fullmatch(item) for item in origins)
        ):
            raise BridgeError("invalid_config", "allowed_origins must contain exact extension origins")
        if len(set(origins)) != len(origins):
            raise BridgeError("invalid_config", "allowed_origins must not contain duplicates")

        raw_state_path = raw.get("state_path")
        if raw_state_path is None:
            state_path = config_path.parent / "native-host-arm.json"
        else:
            state_path = Path(str(raw_state_path)).expanduser().resolve(strict=False)
        state_path = state_path.resolve(strict=False)
        if state_path.parent != config_path.parent:
            raise BridgeError("invalid_config", "state_path must stay beside the native host config")

        max_wait_seconds = float(raw.get("max_wait_seconds", 30.0))
        if not 0.0 <= max_wait_seconds <= _MAX_WAIT_SECONDS:
            raise BridgeError("invalid_config", "max_wait_seconds must be between 0 and 120")
        max_message_bytes = raw.get("max_message_bytes", DEFAULT_MAX_MESSAGE_BYTES)
        if (
            isinstance(max_message_bytes, bool)
            or not isinstance(max_message_bytes, int)
            or not 1024 <= max_message_bytes <= DEFAULT_MAX_MESSAGE_BYTES
        ):
            raise BridgeError("invalid_config", "max_message_bytes must be between 1024 and 1048576")

        return cls(
            bridge_config_path=bridge_config_path,
            allowed_origins=tuple(origins),
            state_path=state_path,
            max_wait_seconds=max_wait_seconds,
            max_message_bytes=max_message_bytes,
        )


@dataclass(frozen=True)
class NativeArmStatus:
    armed: bool
    armed_until: str | None
    generation_id: str | None


class NativeArmStore:
    def __init__(self, path: str | Path, *, now_fn: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.now_fn = now_fn

    def arm(self, *, minutes: int) -> NativeArmStatus:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 60:
            raise BridgeError("invalid_payload", "Arm duration must be between 1 and 60 minutes")
        until = self.now_fn() + timedelta(minutes=minutes)
        generation_id = secrets.token_hex(16)
        payload = {
            "schema": NATIVE_ARM_SCHEMA,
            "armed": True,
            "armed_until": _utc_text(until),
            "generation_id": generation_id,
        }
        _atomic_json_write(self.path, payload)
        return NativeArmStatus(True, payload["armed_until"], generation_id)

    def disarm(self) -> NativeArmStatus:
        payload = {
            "schema": NATIVE_ARM_SCHEMA,
            "armed": False,
            "armed_until": None,
            "generation_id": secrets.token_hex(16),
        }
        _atomic_json_write(self.path, payload)
        return NativeArmStatus(False, None, payload["generation_id"])

    def status(self) -> NativeArmStatus:
        if not self.path.exists():
            return NativeArmStatus(False, None, None)
        if self.path.is_symlink() or not self.path.is_file():
            raise BridgeError("invalid_config", "Native host arm state must be a regular file")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise BridgeError("invalid_config", "Native host arm state is invalid JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema") != NATIVE_ARM_SCHEMA:
            raise BridgeError("unsupported_schema", "Native host arm state schema is unsupported")
        generation_id = raw.get("generation_id")
        if generation_id is not None and not isinstance(generation_id, str):
            raise BridgeError("invalid_config", "Native host generation_id must be a string")
        if raw.get("armed") is not True:
            return NativeArmStatus(False, None, generation_id)
        armed_until = require_string(raw, "armed_until")
        until = parse_strict_utc_timestamp(armed_until, field="armed_until")
        if self.now_fn() >= until:
            return NativeArmStatus(False, armed_until, generation_id)
        return NativeArmStatus(True, armed_until, generation_id)


class NativeHostService:
    def __init__(
        self,
        native_config: NativeHostConfig,
        *,
        origin: str,
        now_fn: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if origin not in native_config.allowed_origins:
            raise BridgeError("policy_denied", "Native messaging origin is not allowed")
        self.native_config = native_config
        self.origin = origin
        self.now_fn = now_fn
        self.sleeper = sleeper
        self.bridge_config = BridgeConfig.from_json(native_config.bridge_config_path)
        if not self.bridge_config.direct_spool_enabled:
            raise BridgeError("invalid_config", "Direct spool must be enabled for Native Messaging")
        self.arm_store = NativeArmStore(native_config.state_path, now_fn=now_fn)
        self.writer = LocalSpoolWriter(self.bridge_config.direct_spool_dir)
        self.results = LocalResultSink(self.bridge_config.direct_result_dir)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = require_string(request, "request_id")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise BridgeError("invalid_payload", "request_id has an unsafe format")
        if request.get("schema") != NATIVE_REQUEST_SCHEMA:
            raise BridgeError("unsupported_schema", "Native request schema is unsupported")
        action = require_string(request, "action")

        if action == "status":
            return self._response(request_id, "status", arm=self._arm_payload())
        if action not in {"submit", "result"}:
            raise BridgeError("policy_denied", "Native action is not allowed")

        arm = self.arm_store.status()
        if not arm.armed:
            raise BridgeError("policy_denied", "Native host is DISARMED or its TTL expired")

        wait_seconds = request.get("wait_seconds", self.native_config.max_wait_seconds)
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            raise BridgeError("invalid_payload", "wait_seconds must be a number")
        wait_seconds = float(wait_seconds)
        if not 0.0 <= wait_seconds <= self.native_config.max_wait_seconds:
            raise BridgeError("invalid_payload", "wait_seconds exceeds the configured maximum")

        if action == "submit":
            envelope = request.get("envelope")
            if not isinstance(envelope, dict) or envelope.get("schema") != LOCAL_ENVELOPE_SCHEMA:
                raise BridgeError("invalid_payload", "submit requires bdb-local-envelope-v1")
            command = envelope.get("command")
            if not isinstance(command, dict):
                raise BridgeError("invalid_payload", "envelope.command must be an object")
            session_id = require_string(command, "session_id")
            validate_session_id(session_id)
            sequence = require_int(command, "sequence")
            if isinstance(sequence, bool) or sequence <= 0:
                raise BridgeError("invalid_payload", "sequence must be a positive integer")
            expected_command_id = command_id_for(session_id, sequence)
            if require_string(command, "command_id") != expected_command_id:
                raise BridgeError("invalid_payload", "command_id does not match session_id and sequence")
            filename = require_string(request, "filename")
            destination = self.writer.submit(envelope, filename=filename)
            wake_signaled = signal_running_bridge(self.bridge_config.runtime_dir)
            result = self._wait_for_result(session_id, sequence, wait_seconds)
            if result is None:
                return self._response(
                    request_id,
                    "accepted",
                    command_id=expected_command_id,
                    filename=destination.name,
                    wake_signaled=wake_signaled,
                    arm=self._arm_payload(),
                )
            return self._response(
                request_id,
                "completed",
                command_id=expected_command_id,
                wake_signaled=wake_signaled,
                result=result,
                arm=self._arm_payload(),
            )

        session_id = require_string(request, "session_id")
        validate_session_id(session_id)
        sequence = require_int(request, "sequence")
        if isinstance(sequence, bool) or sequence <= 0:
            raise BridgeError("invalid_payload", "sequence must be a positive integer")
        result = self._wait_for_result(session_id, sequence, wait_seconds)
        if result is None:
            return self._response(
                request_id,
                "pending",
                command_id=command_id_for(session_id, sequence),
                arm=self._arm_payload(),
            )
        return self._response(
            request_id,
            "completed",
            command_id=command_id_for(session_id, sequence),
            result=result,
            arm=self._arm_payload(),
        )

    def _wait_for_result(self, session_id: str, sequence: int, wait_seconds: float) -> dict[str, Any] | None:
        remote_path = result_path_for(session_id, sequence)
        deadline = time.monotonic() + wait_seconds
        while True:
            content = self.results.read(remote_path)
            if content is not None:
                try:
                    parsed = json.loads(content.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BridgeError("journal_corrupt", "Local result is not strict UTF-8 JSON") from exc
                if not isinstance(parsed, dict):
                    raise BridgeError("journal_corrupt", "Local result root must be an object")
                return parsed
            if time.monotonic() >= deadline:
                return None
            self.sleeper(min(0.05, max(0.0, deadline - time.monotonic())))

    def _arm_payload(self) -> dict[str, Any]:
        status = self.arm_store.status()
        return {
            "armed": status.armed,
            "armed_until": status.armed_until,
            "generation_id": status.generation_id,
        }

    @staticmethod
    def _response(request_id: str, status: str, **payload: Any) -> dict[str, Any]:
        return {
            "schema": NATIVE_RESPONSE_SCHEMA,
            "request_id": request_id,
            "status": status,
            **payload,
        }


def _error_response(request_id: str, exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", "internal_error")
    return {
        "schema": NATIVE_RESPONSE_SCHEMA,
        "request_id": request_id,
        "status": "failed",
        "error": {
            "code": str(code),
            "message": str(exc)[:500],
        },
    }


def run_host(
    *,
    config_path: str | Path,
    origin: str,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
) -> int:
    native_config = NativeHostConfig.from_json(config_path)
    service = NativeHostService(native_config, origin=origin)
    while True:
        request = read_native_message(
            input_stream,
            max_message_bytes=native_config.max_message_bytes,
        )
        if request is None:
            return 0
        request_id = request.get("request_id")
        safe_request_id = request_id if isinstance(request_id, str) and _REQUEST_ID_RE.fullmatch(request_id) else "invalid"
        try:
            response = service.handle(request)
        except Exception as exc:
            response = _error_response(safe_request_id, exc)
        write_native_message(
            output_stream,
            response,
            max_message_bytes=native_config.max_message_bytes,
        )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bdb-native-host")
    parser.add_argument("origin", nargs="?")
    parser.add_argument("--parent-window")
    parser.add_argument("--config")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    origin = args.origin
    if not isinstance(origin, str) or _ORIGIN_RE.fullmatch(origin) is None:
        sys.exit(2)
    config_path = Path(args.config).expanduser().resolve(strict=False) if args.config else default_native_config_path()
    try:
        code = run_host(
            config_path=config_path,
            origin=origin,
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout.buffer,
        )
    except Exception:
        code = 1
    sys.exit(code)
