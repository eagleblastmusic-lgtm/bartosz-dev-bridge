from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Callable

from .ingestion_validate import (
    is_expired,
    parse_command_envelope,
    parse_manifest_envelope,
)
from .journal import Journal
from .journal_ingestion import (
    expire_stale_sessions,
    transition_command_semantic,
    CollisionError,
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


def calculate_raw_sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


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
        try:
            try:
                ingestion = self.ingest_snapshot(snapshot)
            except CollisionError as exc:
                ingestion = exc.report
            validation = self.validate_pending()
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                BridgeErrorCode.INVALID_PAYLOAD,
                f"Unexpected error during polling: {exc}",
            )

        has_blocking = self._journal.has_blocking_ingestion_issues()
        combined = IngestionReport(
            manifests_recorded=ingestion.manifests_recorded,
            commands_discovered=ingestion.commands_discovered,
            commands_validated=validation.commands_validated,
            commands_rejected=validation.commands_rejected,
            commands_expired=validation.commands_expired,
            issues_recorded=ingestion.issues_recorded + validation.issues_recorded,
            blocking_issues=has_blocking,
        )

        error_code = None
        error_message = None
        if has_blocking:
            error_code = BridgeErrorCode.INGESTION_BLOCKED.value
            error_message = "Ingestion blocked due to unresolved issues"

        return PollReport(
            transport_called=True,
            transport_skipped=False,
            transport_succeeded=True,
            snapshot_sha=snapshot.snapshot_sha,
            ingestion=combined,
            error_code=error_code,
            error_message=error_message,
        )

    def ingest_snapshot(self, snapshot: CommandSnapshot) -> IngestionReport:
        manifests_recorded = 0
        commands_discovered = 0
        issues_recorded = 0
        collisions: list[CollisionError] = []

        for document in sorted(snapshot.manifests, key=lambda item: item.path):
            try:
                session_id = parse_manifest_path(document.path)
                try:
                    decoded = document.content.decode("utf-8", errors="strict")
                except UnicodeDecodeError as decode_exc:
                    raise BridgeError(
                        BridgeErrorCode.INVALID_PAYLOAD,
                        f"Manifest content must be valid UTF-8: {decode_exc}",
                    )

                parsed = parse_manifest_envelope(decoded, source_path=document.path)
                raw_hash = calculate_raw_sha256(document.content)
                manifest_json = canonical_json(parsed)
                manifest_hash = sha256_text(manifest_json)

                outcome = self._journal.record_session_manifest(
                    source_id=self._source_id,
                    snapshot_sha=snapshot.snapshot_sha,
                    source_path=document.path,
                    session_id=session_id,
                    manifest_commit_sha=document.document_commit_sha,
                    raw_content=decoded,
                    manifest_json=manifest_json,
                    manifest_sha256=manifest_hash,
                    raw_sha256=raw_hash,
                    repository_id=require_string(parsed, "repository_id"),
                    base_sha=validate_base_sha(require_string(parsed, "base_sha")),
                    created_remote_at=require_string(parsed, "created_at"),
                    expires_at=require_string(parsed, "expires_at"),
                )
                record, created, promotion_outcome = outcome
                if created:
                    manifests_recorded += 1
                commands_discovered += promotion_outcome.promoted_count
                issues_recorded += promotion_outcome.issues_created
            except CollisionError as exc:
                collisions.append(exc)
                if exc.promotion_outcome is not None:
                    commands_discovered += exc.promotion_outcome.promoted_count
                    issues_recorded += exc.promotion_outcome.issues_created
                else:
                    if exc.issue_created:
                        issues_recorded += 1
            except Exception as exc:
                err_code = getattr(exc, "code", BridgeErrorCode.INVALID_PAYLOAD.value)
                raw_hash = calculate_raw_sha256(document.content)
                issue_outcome = self._journal.record_ingestion_issue(
                    source_id=self._source_id,
                    source_path=document.path,
                    snapshot_sha=snapshot.snapshot_sha,
                    raw_sha256=raw_hash,
                    error_code=str(err_code),
                    detail=str(exc),
                    blocking=False,
                    document_commit_sha=document.document_commit_sha,
                )
                if issue_outcome is not None:
                    issue, created = issue_outcome
                    if created:
                        issues_recorded += 1

        command_docs = sorted(snapshot.commands, key=lambda item: item.path)
        for document in command_docs:
            try:
                session_id, sequence = parse_command_path(document.path)
                raw_hash = calculate_raw_sha256(document.content)

                outcome = self._journal.record_ingested_command(
                    source_id=self._source_id,
                    snapshot_sha=snapshot.snapshot_sha,
                    source_path=document.path,
                    session_id=session_id,
                    sequence=sequence,
                    document_commit_sha=document.document_commit_sha,
                    raw_content=document.content,
                    raw_sha256_value=raw_hash,
                )
                if outcome is not None:
                    record, created, issues_created = outcome
                    if record is not None and created:
                        commands_discovered += 1
                    issues_recorded += issues_created
            except CollisionError as exc:
                collisions.append(exc)
                if exc.issue_created:
                    issues_recorded += 1
            except Exception as exc:
                session_id = None
                command_id = None
                try:
                    sid, seq = parse_command_path(document.path)
                    session_id = sid
                    command_id = command_id_for(sid, seq)
                except Exception:
                    pass
                raw_hash = calculate_raw_sha256(document.content)
                err_code = getattr(exc, "code", BridgeErrorCode.INVALID_PAYLOAD.value)
                issue_outcome = self._journal.record_ingestion_issue(
                    source_id=self._source_id,
                    source_path=document.path,
                    snapshot_sha=snapshot.snapshot_sha,
                    raw_sha256=raw_hash,
                    error_code=str(err_code),
                    detail=str(exc),
                    blocking=False,
                    document_commit_sha=document.document_commit_sha,
                    session_id=session_id,
                    command_id=command_id,
                )
                if issue_outcome is not None:
                    issue, created = issue_outcome
                    if created:
                        issues_recorded += 1

        report = IngestionReport(
            manifests_recorded=manifests_recorded,
            commands_discovered=commands_discovered,
            commands_validated=0,
            commands_rejected=0,
            commands_expired=0,
            issues_recorded=issues_recorded,
            blocking_issues=self._journal.has_blocking_ingestion_issues(),
        )
        if collisions:
            collisions[0].report = report
            raise collisions[0]
        return report

    def validate_pending(self, snapshot_sha: str | None = None) -> IngestionReport:
        now = self._now_fn()
        now_dt = parse_strict_utc_timestamp(now, field="now")
        validated = 0
        rejected = 0
        expired = 0
        issues_recorded = 0

        expire_stale_sessions(self._journal, now_dt=now_dt)

        for command in self._journal.list_discovered_commands():
            curr = self._journal.get_command(command.command_id)
            if curr is None or curr.state != CommandState.DISCOVERED:
                continue

            session_ingestion = self._journal.get_session_ingestion(command.session_id)
            if session_ingestion is None:
                continue

            ingestion_meta = self._journal.get_command_ingestion(command.command_id)
            if ingestion_meta is None:
                raise BridgeError(
                    BridgeErrorCode.JOURNAL_CONFLICT,
                    f"Command ingestion metadata missing for discovered command {command.command_id}",
                )
            if ingestion_meta.source_id != self._source_id:
                continue

            source_id = ingestion_meta.source_id
            source_path = ingestion_meta.source_path
            actual_snapshot_sha = ingestion_meta.snapshot_sha
            actual_raw_sha256 = ingestion_meta.raw_sha256
            actual_doc_sha = ingestion_meta.document_commit_sha

            if is_expired(session_ingestion.expires_at, now=now_dt):
                self._journal.expire_session_and_pending_commands(command.session_id, session_ingestion.expires_at)
                expired += 1
                continue

            try:
                parsed = parse_command_envelope(command.command_json, source_path=source_path)
                expected_rev = parsed.get("expected_revision")
                if isinstance(expected_rev, bool):
                    raise BridgeError(
                        BridgeErrorCode.INVALID_REVISION,
                        "Command expected_revision must not be a boolean",
                    )
            except BridgeError as exc:
                issue_created = self._journal.reject_command_during_validation(
                    command.command_id,
                    error_code=str(exc.code),
                    detail=str(exc),
                    source_id=source_id,
                    source_path=source_path,
                    snapshot_sha=actual_snapshot_sha,
                    raw_sha256=actual_raw_sha256,
                    document_commit_sha=actual_doc_sha,
                )
                if issue_created:
                    issues_recorded += 1
                rejected += 1
                continue

            expires_at = require_string(parsed, "expires_at")
            if is_expired(expires_at, now=now_dt):
                self._journal.expire_command_during_validation(command.command_id)
                expired += 1
                continue

            if command.sequence > 1:
                count = self._journal.count_session_commands_before(command.session_id, command.sequence)
                if count != command.sequence - 1:
                    continue

            canonical = canonical_json(parsed)
            canonical_sha = sha256_text(canonical)
            expected_revision = parsed.get("expected_revision")
            expected_state_hash = parsed.get("expected_state_hash")
            created_remote_at = require_string(parsed, "created_at")

            self._journal.validate_and_update_command(
                command.command_id,
                command_json=canonical,
                command_sha256=canonical_sha,
                expected_revision=expected_revision,
                expected_state_hash=expected_state_hash,
                created_remote_at=created_remote_at,
                expires_at=expires_at,
                snapshot_sha=actual_snapshot_sha,
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
