from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from bdb_vnext.project_launch import ProjectLaunchQueueAdapter
from bdb_vnext.project_launch_canonical import (
    ProjectLaunchCanonicalError,
    ProjectLaunchCanonicalState,
)

from .native_host import (
    NATIVE_REQUEST_SCHEMA,
    NATIVE_RESPONSE_SCHEMA,
    NATIVE_HOST_VERSION,
    NativeHostConfig,
    NativeHostService,
    _error_response,
    _write_error_diagnostic,
)
from .native_messaging import read_native_message, write_native_message
from .protocol import BridgeError, require_string


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def _uuid_field(request: dict[str, Any], field: str) -> str:
    value = require_string(request, field)
    try:
        uuid.UUID(value)
    except ValueError as error:
        raise BridgeError("invalid_payload", f"{field} must be a UUID") from error
    return value


def _conversation_field(request: dict[str, Any]) -> str:
    value = require_string(request, "conversation_id")
    if _CONVERSATION_ID_RE.fullmatch(value) is None:
        raise BridgeError("invalid_payload", "conversation_id has an unsafe format")
    return value


class ProjectLauncherNativeHostService:
    """Add a leased prompt handoff without widening repository operations."""

    def __init__(
        self,
        native_config: NativeHostConfig,
        *,
        origin: str,
        canonical_state: ProjectLaunchCanonicalState | None = None,
    ) -> None:
        self._base = NativeHostService(native_config, origin=origin)
        queue_path = native_config.state_path.parent / "project-launch-queue.json"
        self._queue = ProjectLaunchQueueAdapter(queue_path)
        # Rich vNext launches are only valid when the Native Host and Project
        # Center share the same runtime/control directory. Legacy launches do
        # not touch canonical Project Memory and keep their old behavior.
        runtime_root = queue_path.parent.parent
        self._canonical = canonical_state or ProjectLaunchCanonicalState(runtime_root)

    def _is_canonical_launch(self, launch) -> bool:
        try:
            return self._canonical.is_canonical_launch(launch)
        except ProjectLaunchCanonicalError as exc:
            raise BridgeError(exc.code, str(exc)) from exc

    def _canonical_acknowledged(self, launch) -> bool:
        try:
            return self._canonical.is_acknowledged(launch)
        except ProjectLaunchCanonicalError as exc:
            raise BridgeError(exc.code, str(exc)) from exc

    def _activate_claimed(self, launch) -> None:
        try:
            self._canonical.activate_claimed(launch)
        except ProjectLaunchCanonicalError as exc:
            raise BridgeError(exc.code, str(exc)) from exc

    def _acknowledge_canonical(self, launch, conversation_id: str) -> None:
        try:
            self._canonical.acknowledge_delivery(launch, conversation_id)
        except ProjectLaunchCanonicalError as exc:
            raise BridgeError(exc.code, str(exc)) from exc

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("schema") != NATIVE_REQUEST_SCHEMA:
            return self._base.handle(request)
        action = request.get("action")
        if action not in {
            "project_launch_peek",
            "project_launch_claim",
            "project_launch_ack",
        }:
            return self._base.handle(request)

        request_id = require_string(request, "request_id")
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise BridgeError("invalid_payload", "request_id has an unsafe format")
        client_version = request.get("client_version")
        if client_version is not None and client_version != NATIVE_HOST_VERSION:
            raise BridgeError("version_mismatch", "Browser extension and Native Host versions differ")
        arm = self._base.arm_store.status()
        if not arm.armed:
            raise BridgeError("policy_denied", "Native host is DISARMED or its TTL expired")

        if action == "project_launch_peek":
            launch = self._queue.peek()
            return {
                "schema": NATIVE_RESPONSE_SCHEMA,
                "host_version": NATIVE_HOST_VERSION,
                "request_id": request_id,
                "status": "empty" if launch is None else "project_launch",
                "launch": None if launch is None else launch.to_dict(),
                "arm": self._base._arm_payload(),
            }

        launch_id = _uuid_field(request, "launch_id")
        claim_id = _uuid_field(request, "claim_id")
        if action == "project_launch_claim":
            launch = self._queue.claim(
                launch_id=launch_id,
                claim_id=claim_id,
                lease_seconds=45,
            )
            if launch is not None and self._is_canonical_launch(launch):
                # Crash recovery: canonical ACK is authoritative. If the
                # process stopped after Project Memory ACK but before queue
                # clear, consume the stale projection without returning the
                # prompt to Browser a second time.
                if self._canonical_acknowledged(launch):
                    self._queue.acknowledge(launch_id=launch_id, claim_id=claim_id)
                    return {
                        "schema": NATIVE_RESPONSE_SCHEMA,
                        "host_version": NATIVE_HOST_VERSION,
                        "request_id": request_id,
                        "status": "already_acknowledged",
                        "launch": None,
                        "claim_id": claim_id,
                        "arm": self._base._arm_payload(),
                    }
                # A retry binding can legitimately still have the old task
                # projection "blocked". Browser ownership is the bounded point
                # at which that exact current binding becomes active again.
                self._activate_claimed(launch)
            return {
                "schema": NATIVE_RESPONSE_SCHEMA,
                "host_version": NATIVE_HOST_VERSION,
                "request_id": request_id,
                "status": "claimed" if launch is not None else "busy_or_missing",
                "launch": None if launch is None else launch.to_dict(),
                "claim_id": claim_id,
                "arm": self._base._arm_payload(),
            }

        # ACK may only mutate canonical state for the exact live lease owner.
        if not self._queue.claim_matches(launch_id=launch_id, claim_id=claim_id):
            acknowledged = False
        else:
            launch = self._queue.peek()
            if launch is None or launch.launch_id != launch_id:
                acknowledged = False
            elif self._is_canonical_launch(launch):
                conversation_id = _conversation_field(request)
                # Durably bind the conversation and ACK Project Memory before
                # deleting the transport projection. A crash after this point
                # is recovered by the claim path above without duplicate prompt
                # delivery.
                self._acknowledge_canonical(launch, conversation_id)
                self._queue.acknowledge(launch_id=launch_id, claim_id=claim_id)
                acknowledged = True
            else:
                # Backward-compatible path for old minimal queue records.
                acknowledged = self._queue.acknowledge(
                    launch_id=launch_id,
                    claim_id=claim_id,
                )
        return {
            "schema": NATIVE_RESPONSE_SCHEMA,
            "host_version": NATIVE_HOST_VERSION,
            "request_id": request_id,
            "status": "acknowledged" if acknowledged else "not_found_or_not_owner",
            "launch_id": launch_id,
            "claim_id": claim_id,
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
            _write_error_diagnostic(native_config, safe_request_id, error)
            response = _error_response(safe_request_id, error)
        write_native_message(
            output_stream,
            response,
            max_message_bytes=native_config.max_message_bytes,
        )
