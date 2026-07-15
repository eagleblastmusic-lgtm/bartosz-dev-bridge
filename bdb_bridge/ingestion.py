from __future__ import annotations

from datetime import datetime
from typing import Callable

from .ingestion_validate import (
    is_expired,
    parse_command_envelope,
    parse_manifest_envelope,
    raw_sha256,
)
from .journal import Journal
from .journal_ingestion import (
    expire_stale_sessions,
    transition_command_semantic,
    update_command_canonical,
)
from .models import (
    BridgeErrorCode,
    CommandState,
    IngestionReport,
    PollReport,
    SessionState,
)
from .protocol import (
    BridgeError,
    command_id_for,
    command_path_for,
    parse_command_path,
    parse_manifest_path,
    parse_strict_utc_timestamp,
    require_string,
    validate_base_sha,
)
from .serializers import canonical_json, sha256_text
from .transport import CommandSnapshot, CommandTransport


class CommandIngestor:
    def __init__(
        self,
        journal: Journal,
        transport: CommandTransport,
        *,
        source_id: str = "commands",
        now_fn: Callable[[], str] | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
    ) -> None:
        self._journal = journal
        self._transport = transport
        self._source_id = source_id
        self._now_fn = now_fn or journal._now_fn
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max

    def poll_once(self) -> PollReport:
        now = self._now_fn()
        now_dt = parse_strict_utc_timestamp(now, field="now")
        source = self._journal.get_ingestion_source(self._source_id)
        if source.next_attempt_at is not None:
            next_dt = parse_strict_utc_timestamp(source.next_attempt_at, field="next_attempt_at")
            if now_dt < next_dt:
                return PollReport(
                    transport_called=False,
                    transport_skipped=True,
                    transport_succeeded=False,
                    snapshot_sha=None,
                    ingestion=None,
                    error_code=None,
                    error_message=None,
                )

        try:
            snapshot = self._transport.fetch_snapshot()
        except BridgeError as exc:
            if exc.code in {
                BridgeErrorCode.TRANSPORT_UNAVAILABLE.value,
                BridgeErrorCode.GIT_ERROR.value,
            }:
                self._journal.record_transport_failure(
                    self._source_id,
                    str(exc),
                    base_delay=self._backoff_base,
                    max_delay=self._backoff_max,
                )
                return PollReport(
                    transport_called=True,
                    transport_skipped=False,
                    transport_succeeded=False,
                    snapshot_sha=None,
                    ingestion=None,
                    error_code=BridgeErrorCode.TRANSPORT_UNAVAILABLE.value,
                    error_message=str(exc),
                )
            raise
        except Exception as exc:
            self._journal.record_transport_failure(
                self._source_id,
                str(exc),
                base_delay=self._backoff_base,
                max_delay=self._backoff_max,
            )
            return PollReport(
                transport_called=True,
                transport_skipped=False,
                transport_succeeded=False,
                snapshot_sha=None,
                ingestion=None,
                error_code=BridgeErrorCode.TRANSPORT_UNAVAILABLE.value,
                error_message=str(exc),
            )

        self._journal.record_transport_success(self._source_id, snapshot.snapshot_sha)
        ingestion = self.ingest_snapshot(snapshot)
        validation = self.validate_pending()
        combined = IngestionReport(
            manifests_recorded=ingestion.manifests_recorded,
            commands_discovered=ingestion.commands_discovered,
            commands_validated=validation.commands_validated,
            commands_rejected=validation.commands_rejected,
            commands_expired=validation.commands_expired,
            issues_recorded=ingestion.issues_recorded + validation.issues_recorded,
            blocking_issues=self._journal.has_blocking_ingestion_issues(),
        )
        return PollReport(
            transport_called=True,
            transport_skipped=False,
            transport_succeeded=True,
            snapshot_sha=snapshot.snapshot_sha,
            ingestion=combined,
            error_code=None,
            error_message=None,
        )

    def ingest_snapshot(self, snapshot: CommandSnapshot) -> IngestionReport:
        manifests_recorded = 0
        commands_discovered = 0
        issues_recorded = 0

        for document in sorted(snapshot.manifests, key=lambda item: item.path):
            try:
                session_id = parse_manifest_path(document.path)
                parsed = parse_manifest_envelope(document.content, source_path=document.path)
                raw_hash = raw_sha256(document.content)
                manifest_json = canonical_json(parsed)
                manifest_hash = sha256_text(manifest_json)
                self._journal.record_session_manifest(
                    source_id=self._source_id,
                    snapshot_sha=snapshot.snapshot_sha,
                    source_path=document.path,
                    session_id=session_id,
                    manifest_commit_sha=document.document_commit_sha,
                    raw_content=document.content,
                    manifest_json=manifest_json,
                    manifest_sha256=manifest_hash,
                    raw_sha256=raw_hash,
                    repository_id=require_string(parsed, "repository_id"),
                    base_sha=validate_base_sha(require_string(parsed, "base_sha")),
                    created_remote_at=require_string(parsed, "created_at"),
                    expires_at=require_string(parsed, "expires_at"),
                )
                manifests_recorded += 1
            except BridgeError as exc:
                if exc.code in {
                    BridgeErrorCode.SESSION_ID_COLLISION.value,
                    BridgeErrorCode.COMMAND_ID_COLLISION.value,
                    BridgeErrorCode.SEQUENCE_COLLISION.value,
                }:
                    raise
                raw_hash = raw_sha256(document.content)
                issue = self._journal.record_ingestion_issue(
                    source_id=self._source_id,
                    source_path=document.path,
                    snapshot_sha=snapshot.snapshot_sha,
                    raw_sha256=raw_hash,
                    error_code=str(exc.code),
                    detail=str(exc),
                    blocking=exc.code
                    in {
                        BridgeErrorCode.SESSION_ID_COLLISION.value,
                        BridgeErrorCode.COMMAND_ID_COLLISION.value,
                        BridgeErrorCode.SEQUENCE_COLLISION.value,
                    },
                    document_commit_sha=document.document_commit_sha,
                )
                if issue is not None:
                    issues_recorded += 1

        command_docs = sorted(
            snapshot.commands,
            key=lambda item: (
                parse_command_path(item.path)[0],
                parse_command_path(item.path)[1],
                item.path,
            ),
        )
        for document in command_docs:
            try:
                session_id, sequence = parse_command_path(document.path)
                raw_hash = raw_sha256(document.content)
                record = self._journal.record_ingested_command(
                    source_id=self._source_id,
                    snapshot_sha=snapshot.snapshot_sha,
                    source_path=document.path,
                    session_id=session_id,
                    sequence=sequence,
                    document_commit_sha=document.document_commit_sha,
                    raw_content=document.content,
                    raw_sha256_value=raw_hash,
                )
                if record is not None:
                    commands_discovered += 1
            except BridgeError as exc:
                if exc.code in {
                    BridgeErrorCode.SESSION_ID_COLLISION.value,
                    BridgeErrorCode.COMMAND_ID_COLLISION.value,
                    BridgeErrorCode.SEQUENCE_COLLISION.value,
                }:
                    raise
                session_id: str | None = None
                command_id: str | None = None
                try:
                    sid, seq = parse_command_path(document.path)
                    session_id = sid
                    command_id = command_id_for(sid, seq)
                except BridgeError:
                    pass
                raw_hash = raw_sha256(document.content)
                issue = self._journal.record_ingestion_issue(
                    source_id=self._source_id,
                    source_path=document.path,
                    snapshot_sha=snapshot.snapshot_sha,
                    raw_sha256=raw_hash,
                    error_code=str(exc.code),
                    detail=str(exc),
                    blocking=exc.code
                    in {
                        BridgeErrorCode.SESSION_ID_COLLISION.value,
                        BridgeErrorCode.COMMAND_ID_COLLISION.value,
                        BridgeErrorCode.SEQUENCE_COLLISION.value,
                    },
                    document_commit_sha=document.document_commit_sha,
                    session_id=session_id,
                    command_id=command_id,
                )
                if issue is not None:
                    issues_recorded += 1

        return IngestionReport(
            manifests_recorded=manifests_recorded,
            commands_discovered=commands_discovered,
            commands_validated=0,
            commands_rejected=0,
            commands_expired=0,
            issues_recorded=issues_recorded,
            blocking_issues=self._journal.has_blocking_ingestion_issues(),
        )

    def validate_pending(self) -> IngestionReport:
        now = self._now_fn()
        now_dt = parse_strict_utc_timestamp(now, field="now")
        validated = 0
        rejected = 0
        expired = 0
        issues_recorded = 0

        expire_stale_sessions(self._journal, now_dt=now_dt)

        for command in self._journal.list_discovered_commands():
            session_ingestion = self._journal.get_session_ingestion(command.session_id)
            if session_ingestion is None:
                continue

            source_path = command_path_for(command.session_id, command.sequence)
            ingestion_meta = self._journal.get_command_ingestion(command.command_id)
            if ingestion_meta is not None:
                source_path = ingestion_meta.source_path

            if is_expired(session_ingestion.expires_at, now=now_dt):
                transition_command_semantic(
                    self._journal,
                    command.command_id,
                    CommandState.DISCOVERED,
                    CommandState.EXPIRED,
                    semantic_event_type="command.expired",
                )
                expired += 1
                continue

            try:
                parsed = parse_command_envelope(command.command_json, source_path=source_path)
            except BridgeError as exc:
                if exc.code == BridgeErrorCode.UNSUPPORTED_SCHEMA.value:
                    new_state = CommandState.REJECTED
                    semantic = "command.rejected"
                    rejected += 1
                elif exc.code in {
                    BridgeErrorCode.INVALID_JSON.value,
                    BridgeErrorCode.INVALID_PAYLOAD.value,
                }:
                    new_state = CommandState.REJECTED
                    semantic = "command.rejected"
                    rejected += 1
                else:
                    new_state = CommandState.REJECTED
                    semantic = "command.rejected"
                    rejected += 1
                transition_command_semantic(
                    self._journal,
                    command.command_id,
                    CommandState.DISCOVERED,
                    new_state,
                    semantic_event_type=semantic,
                )
                issue = self._journal.record_ingestion_issue(
                    source_id=self._source_id,
                    source_path=source_path,
                    snapshot_sha="",
                    raw_sha256=command.command_sha256,
                    error_code=str(exc.code),
                    detail=str(exc),
                    blocking=False,
                    session_id=command.session_id,
                    command_id=command.command_id,
                )
                if issue is not None:
                    issues_recorded += 1
                continue

            expires_at = require_string(parsed, "expires_at")
            if is_expired(expires_at, now=now_dt):
                transition_command_semantic(
                    self._journal,
                    command.command_id,
                    CommandState.DISCOVERED,
                    CommandState.EXPIRED,
                    semantic_event_type="command.expired",
                )
                expired += 1
                continue

            if command.sequence > 1:
                predecessor = self._journal.get_command_by_sequence(
                    command.session_id,
                    command.sequence - 1,
                )
                if predecessor is None:
                    continue

            canonical = canonical_json(parsed)
            if command.command_json != canonical or command.command_sha256 != sha256_text(canonical):
                update_command_canonical(
                    self._journal,
                    command.command_id,
                    command_json=canonical,
                    command_sha256=sha256_text(canonical),
                )

            transition_command_semantic(
                self._journal,
                command.command_id,
                CommandState.DISCOVERED,
                CommandState.VALIDATED,
                semantic_event_type="command.validated",
            )
            validated += 1

        return IngestionReport(
            manifests_recorded=0,
            commands_discovered=0,
            commands_validated=validated,
            commands_rejected=rejected,
            commands_expired=expired,
            issues_recorded=issues_recorded,
            blocking_issues=self._journal.has_blocking_ingestion_issues(),
        )
