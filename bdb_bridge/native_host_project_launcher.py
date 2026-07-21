from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .native_host import (
    NATIVE_REQUEST_SCHEMA,
    NATIVE_RESPONSE_SCHEMA,
    NativeHostConfig,
    NativeHostService,
    _error_response,
)
from .native_messaging import read_native_message, write_native_message
from .project_launch import ProjectLaunchQueue
from .protocol import BridgeError, require_string


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProjectLauncherNativeHostService:
    """Add a two-phase prompt handoff without widening repository operations."""

    def __init__(self, native_config: NativeHostConfig, *, origin: str) -> None:
        self._base = NativeHostService(native_config, origin=origin)
        self._queue = ProjectLaunchQueue(native_config.state_path.parent / "project-launch-queue.json")

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("schema") != NATIVE_REQUEST_SCHEMA:
            return self._base.handle(request)
        action = request.get("action")
        if action not in {"project_launch_peek", "project_launch_ack"}:
            return self._base.handle(request)

        request_id = require_string(request, "request_id")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise BridgeError("invalid_payload", "request_id has an unsafe format")
        arm = self._base.arm_store.status()
        if not arm.armed:
            raise BridgeError("policy_denied", "Native host is DISARMED or its TTL expired")

        if action == "project_launch_peek":
            launch = self._queue.peek()
            return {
                "schema": NATIVE_RESPONSE_SCHEMA,
                "request_id": request_id,
                "status": "empty" if launch is None else "project_launch",
                "launch": None if launch is None else launch.to_dict(),
                "arm": self._base._arm_payload(),
            }

        launch_id = require_string(request, "launch_id")
        try:
            uuid.UUID(launch_id)
        except ValueError as error:
            raise BridgeError("invalid_payload", "launch_id must be a UUID") from error
        acknowledged = self._queue.acknowledge(launch_id)
        return {
            "schema": NATIVE_RESPONSE_SCHEMA,
            "request_id": request_id,
            "status": "acknowledged" if acknowledged else "not_found",
            "launch_id": launch_id,
            "arm": self._base._arm_payload(),
        }


def run_project_launcher_host(
    *,
    config_path: str | Path,
    origin: str,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
) -> int:
    native_config = NativeHostConfig.from_json(config_path)
    service = ProjectLauncherNativeHostService(native_config, origin=origin)
    while True:
        request = read_native_message(
            input_stream,
            max_message_bytes=native_config.max_message_bytes,
        )
        if request is None:
            return 0
        request_id = request.get("request_id")
        safe_request_id = (
            request_id
            if isinstance(request_id, str) and _REQUEST_ID_RE.fullmatch(request_id)
            else "invalid"
        )
        try:
            response = service.handle(request)
        except Exception as error:
            response = _error_response(safe_request_id, error)
        write_native_message(
            output_stream,
            response,
            max_message_bytes=native_config.max_message_bytes,
        )
