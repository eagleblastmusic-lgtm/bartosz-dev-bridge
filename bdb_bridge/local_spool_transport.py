from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .protocol import (
    BridgeError,
    command_path_for,
    manifest_path_for,
    parse_strict_utc_timestamp,
    require_int,
    require_string,
)
from .serializers import canonical_json
from .transport import CommandSnapshot, RemoteDocument


LOCAL_ENVELOPE_SCHEMA = "bdb-local-envelope-v1"
_MAX_FILES = 100
_MAX_FILE_BYTES = 1 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")


def _sha1(*parts: bytes) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError("invalid_payload", f"{field} must be an object")
    return value


class LocalSpoolTransport:
    """Read immutable, atomically published command envelopes from a local spool.

    Producers must write a temporary file and publish it with ``os.replace``.  The
    transport intentionally reads only direct ``*.json`` children, never follows
    symlinks, and never removes operator evidence.  Durable Journal identity makes
    repeated reads idempotent.
    """

    def __init__(self, inbox_dir: str | Path) -> None:
        self.inbox_dir = Path(inbox_dir).expanduser().resolve(strict=False)

    def fetch_snapshot(self) -> CommandSnapshot:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        candidates = sorted(self.inbox_dir.glob("*.json"), key=lambda path: path.name)
        if len(candidates) > _MAX_FILES:
            raise BridgeError("invalid_payload", "Local spool contains too many envelopes")

        total = 0
        raw_files: list[tuple[str, bytes]] = []
        for path in candidates:
            if not _SAFE_NAME_RE.fullmatch(path.name):
                raise BridgeError("unsafe_path", f"Unsafe local spool filename: {path.name}")
            if path.is_symlink() or not path.is_file():
                raise BridgeError("unsafe_path", f"Local spool entry must be a regular file: {path.name}")
            before = path.stat()
            if before.st_size > _MAX_FILE_BYTES:
                raise BridgeError("invalid_payload", f"Local spool envelope is too large: {path.name}")
            data = path.read_bytes()
            after = path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or len(data) != after.st_size
            ):
                raise BridgeError("transport_unavailable", f"Local spool envelope changed while reading: {path.name}")
            total += len(data)
            if total > _MAX_TOTAL_BYTES:
                raise BridgeError("invalid_payload", "Local spool snapshot exceeds the total size limit")
            raw_files.append((path.name, data))

        manifests: dict[str, RemoteDocument] = {}
        commands: dict[str, RemoteDocument] = {}
        snapshot_digest = hashlib.sha1()

        for filename, raw in raw_files:
            snapshot_digest.update(filename.encode("utf-8"))
            snapshot_digest.update(b"\0")
            snapshot_digest.update(raw)
            snapshot_digest.update(b"\0")
            try:
                decoded = raw.decode("utf-8", errors="strict")
                envelope = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BridgeError("invalid_payload", f"Invalid local spool JSON in {filename}: {exc}") from exc
            envelope = _require_mapping(envelope, "envelope")
            if envelope.get("schema") != LOCAL_ENVELOPE_SCHEMA:
                raise BridgeError("unsupported_schema", f"Unsupported local envelope schema in {filename}")

            submitted_at = require_string(envelope, "submitted_at")
            parse_strict_utc_timestamp(submitted_at, field="submitted_at")
            manifest = _require_mapping(envelope.get("manifest"), "manifest")
            command = _require_mapping(envelope.get("command"), "command")

            manifest_session = require_string(manifest, "session_id")
            command_session = require_string(command, "session_id")
            if manifest_session != command_session:
                raise BridgeError("invalid_payload", "Local manifest and command session_id must match")
            sequence = require_int(command, "sequence")
            manifest_path = manifest_path_for(manifest_session)
            command_path = command_path_for(command_session, sequence)

            manifest_bytes = canonical_json(manifest).encode("utf-8")
            command_bytes = canonical_json(command).encode("utf-8")
            manifest_doc = RemoteDocument(
                path=manifest_path,
                content=manifest_bytes,
                document_commit_sha=_sha1(raw, b"manifest"),
                document_committed_at=submitted_at,
            )
            command_doc = RemoteDocument(
                path=command_path,
                content=command_bytes,
                document_commit_sha=_sha1(raw, b"command"),
                document_committed_at=submitted_at,
            )
            self._insert_exact(manifests, manifest_doc)
            self._insert_exact(commands, command_doc)

        return CommandSnapshot(
            snapshot_sha=snapshot_digest.hexdigest(),
            manifests=tuple(manifests[path] for path in sorted(manifests)),
            commands=tuple(commands[path] for path in sorted(commands)),
        )

    @staticmethod
    def _insert_exact(target: dict[str, RemoteDocument], document: RemoteDocument) -> None:
        existing = target.get(document.path)
        if existing is None:
            target[document.path] = document
            return
        if existing.content != document.content:
            raise BridgeError("journal_conflict", f"Conflicting local documents for {document.path}")


class LocalSpoolWriter:
    """Atomically publish a validated JSON object into the local inbox."""

    def __init__(self, inbox_dir: str | Path) -> None:
        self.inbox_dir = Path(inbox_dir).expanduser().resolve(strict=False)

    def submit(self, envelope: dict[str, Any], *, filename: str) -> Path:
        if not _SAFE_NAME_RE.fullmatch(filename):
            raise BridgeError("unsafe_path", "Local spool filename must be a safe .json basename")
        if envelope.get("schema") != LOCAL_ENVELOPE_SCHEMA:
            raise BridgeError("unsupported_schema", "Local spool writer requires bdb-local-envelope-v1")
        content = canonical_json(envelope).encode("utf-8")
        if len(content) > _MAX_FILE_BYTES:
            raise BridgeError("invalid_payload", "Local spool envelope is too large")

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        destination = self.inbox_dir / filename
        temporary = self.inbox_dir / f".{filename}.{os.getpid()}.tmp"
        if destination.exists():
            existing = destination.read_bytes()
            if existing == content:
                return destination
            raise BridgeError("journal_conflict", f"Local spool destination already exists: {filename}")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            self._fsync_directory()
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return destination

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        try:
            fd = os.open(self.inbox_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
