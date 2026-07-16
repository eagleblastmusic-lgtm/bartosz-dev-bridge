from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .ingestion_validate import is_expired
from .migrations import map_sqlite_error
from .models import CommandState, SessionState, validate_session_transition
from .protocol import BridgeError, parse_strict_utc_timestamp, validate_session_id
from .workspace_types import WorkspaceDisposition, WorkspaceLifecycleState


_FINAL_COMMAND_STATES = frozenset(
    {
        CommandState.RESULT_PUBLISHED.value,
        CommandState.ACKNOWLEDGED.value,
        CommandState.REJECTED.value,
        CommandState.EXPIRED.value,
        CommandState.POLICY_DENIED.value,
        CommandState.STALE_REVISION.value,
        CommandState.STATE_MISMATCH.value,
        CommandState.CANCELLED.value,
    }
)


@dataclass(frozen=True)
class SessionFinalizationOutcome:
    session_id: str
    state: SessionState
    finalized: bool
    idempotent: bool


class SessionFinalizer:
    def __init__(self, journal: object) -> None:
        self.journal = journal

    def _blocking_reasons_in_transaction(
        self,
        session_id: str,
        *,
        include_service_state: bool = True,
    ) -> list[str]:
        conn = self.journal._connection
        reasons: list[str] = []
        if include_service_state and conn.execute(
            "SELECT 1 FROM service_instances WHERE state IN ('running','stopping') LIMIT 1"
        ).fetchone() is not None:
            reasons.append("service is running or stopping")
        rows = conn.execute(
            "SELECT state FROM commands WHERE session_id = ? ORDER BY sequence", (session_id,)
        ).fetchall()
        if not rows:
            reasons.append("session has no commands")
        states = [str(row[0]) for row in rows]
        if CommandState.MANUAL_RECONCILIATION_REQUIRED.value in states:
            reasons.append("manual reconciliation command exists")
        unresolved = sorted({state for state in states if state not in _FINAL_COMMAND_STATES})
        if unresolved:
            reasons.append("unresolved command states: " + ",".join(unresolved))
        for state, count in conn.execute(
            """SELECT state, COUNT(*) FROM outbox WHERE session_id = ?
            AND state IN ('pending','collision') GROUP BY state ORDER BY state""",
            (session_id,),
        ).fetchall():
            reasons.append(f"outbox {state}: {count}")
        if conn.execute(
            "SELECT 1 FROM ingestion_issues WHERE session_id=? AND blocking=1 LIMIT 1",
            (session_id,),
        ).fetchone() is not None:
            reasons.append("blocking ingestion issue exists")
        return reasons

    def _finalize_active_in_transaction(
        self,
        session_id: str,
        now: str,
        *,
        include_service_state: bool,
    ) -> None:
        reasons = self._blocking_reasons_in_transaction(
            session_id,
            include_service_state=include_service_state,
        )
        if "service is running or stopping" in reasons:
            raise BridgeError("instance_already_running", "Session finalization requires service OFFLINE")
        if reasons:
            raise BridgeError(
                "manual_reconciliation_required",
                "Session finalization blocked: " + "; ".join(reasons),
            )

        validate_session_transition(SessionState.ACTIVE, SessionState.COMPLETING)
        first = self.journal._connection.execute(
            "UPDATE sessions SET state=?,updated_at=? WHERE session_id=? AND state=?",
            (SessionState.COMPLETING.value, now, session_id, SessionState.ACTIVE.value),
        )
        if first.rowcount != 1:
            raise BridgeError("journal_conflict", "Session state changed during finalization")
        self.journal._append_event_in_transaction(
            session_id=session_id,
            event_type="session.state_changed",
            payload={"from_state": "active", "to_state": "completing"},
            created_at=now,
        )

        reasons = self._blocking_reasons_in_transaction(
            session_id,
            include_service_state=include_service_state,
        )
        if "service is running or stopping" in reasons:
            raise BridgeError("instance_already_running", "Service became active during finalization")
        if reasons:
            raise BridgeError(
                "manual_reconciliation_required",
                "Session finalization recheck failed: " + "; ".join(reasons),
            )

        validate_session_transition(SessionState.COMPLETING, SessionState.COMPLETED)
        second = self.journal._connection.execute(
            "UPDATE sessions SET state=?,updated_at=? WHERE session_id=? AND state=?",
            (SessionState.COMPLETED.value, now, session_id, SessionState.COMPLETING.value),
        )
        if second.rowcount != 1:
            raise BridgeError("journal_conflict", "Session completion transition failed")
        self.journal._append_event_in_transaction(
            session_id=session_id,
            event_type="session.state_changed",
            payload={"from_state": "completing", "to_state": "completed"},
            created_at=now,
        )
        self._ensure_preserve_in_transaction(session_id, now)

    def finalize(self, session_id: str, *, lock_held: bool) -> SessionFinalizationOutcome:
        validate_session_id(session_id)
        if not lock_held:
            raise BridgeError("instance_lock_failed", "Session finalization requires the shared instance lock")
        self.journal._ensure_open()
        now = self.journal._now_fn()
        try:
            with self.journal._transaction():
                row = self.journal._connection.execute(
                    "SELECT state FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                if row is None:
                    raise BridgeError("journal_conflict", f"Session not found: {session_id}")
                state = SessionState(str(row[0]))
                if state is SessionState.COMPLETED:
                    reasons = self._blocking_reasons_in_transaction(session_id)
                    service_reasons = [
                        reason for reason in reasons if reason == "service is running or stopping"
                    ]
                    if service_reasons:
                        raise BridgeError("instance_already_running", service_reasons[0])
                    self._ensure_preserve_in_transaction(session_id, now)
                    return SessionFinalizationOutcome(session_id, state, False, True)
                if state is not SessionState.ACTIVE:
                    raise BridgeError(
                        "invalid_state_transition",
                        f"Session finalization requires ACTIVE, got {state.value}",
                    )
                self._finalize_active_in_transaction(
                    session_id,
                    now,
                    include_service_state=True,
                )
        except BridgeError:
            raise
        except sqlite3.Error as exc:
            raise map_sqlite_error(exc, context="session finalization") from exc
        return SessionFinalizationOutcome(session_id, SessionState.COMPLETED, True, False)

    def handoff_ready_session(self, service_instance_id: str) -> tuple[str, str] | None:
        """Complete an idle active session only when another valid session is waiting.

        The caller must supply the current service instance identifier from the active
        Bridge loop. A stale database row or an offline scheduler invocation cannot
        authorize the handoff.
        """

        if not isinstance(service_instance_id, str) or not service_instance_id:
            raise BridgeError("invalid_payload", "service_instance_id must be a non-empty string")
        self.journal._ensure_open()
        now = self.journal._now_fn()
        now_dt = parse_strict_utc_timestamp(now, field="now")
        try:
            with self.journal._transaction():
                service_rows = self.journal._connection.execute(
                    """SELECT instance_id,state FROM service_instances
                    WHERE state IN ('running','stopping') ORDER BY started_at,instance_id"""
                ).fetchall()
                if (
                    len(service_rows) != 1
                    or str(service_rows[0][0]) != service_instance_id
                    or str(service_rows[0][1]) != "running"
                ):
                    return None

                if self.journal._connection.execute(
                    "SELECT 1 FROM ingestion_issues WHERE blocking=1 LIMIT 1"
                ).fetchone() is not None:
                    return None

                active_row = self.journal._connection.execute(
                    """SELECT session_id FROM sessions WHERE state=?
                    ORDER BY created_at,session_id LIMIT 1""",
                    (SessionState.ACTIVE.value,),
                ).fetchone()
                if active_row is None:
                    return None
                active_session_id = str(active_row[0])

                candidate_rows = self.journal._connection.execute(
                    """SELECT s.session_id,si.expires_at
                    FROM sessions s
                    JOIN commands c ON c.session_id=s.session_id AND c.sequence=1
                    JOIN session_ingestion si ON si.session_id=s.session_id
                    WHERE s.state=? AND c.state=?
                    ORDER BY s.created_at,s.session_id""",
                    (SessionState.CREATED.value, CommandState.VALIDATED.value),
                ).fetchall()
                waiting_session_id = None
                for candidate_session_id, expires_at in candidate_rows:
                    if not is_expired(str(expires_at), now=now_dt):
                        waiting_session_id = str(candidate_session_id)
                        break
                if waiting_session_id is None:
                    return None

                reasons = self._blocking_reasons_in_transaction(
                    active_session_id,
                    include_service_state=False,
                )
                if reasons:
                    return None

                self._finalize_active_in_transaction(
                    active_session_id,
                    now,
                    include_service_state=False,
                )
                self.journal._append_event_in_transaction(
                    session_id=active_session_id,
                    event_type="session.auto_completed_for_handoff",
                    payload={"next_session_id": waiting_session_id},
                    created_at=now,
                )
                return active_session_id, waiting_session_id
        except BridgeError:
            raise
        except sqlite3.Error as exc:
            raise map_sqlite_error(exc, context="session handoff") from exc

    def _ensure_preserve_in_transaction(self, session_id: str, now: str) -> None:
        workspace = self.journal._connection.execute(
            "SELECT workspace_path,base_sha,revision,state_hash FROM workspaces WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if workspace is None:
            raise BridgeError("journal_conflict", "Session has no registered workspace")
        existing = self.journal._connection.execute(
            """SELECT workspace_path,base_sha,expected_revision,expected_state_hash,
            disposition,state FROM workspace_lifecycle WHERE session_id=?""",
            (session_id,),
        ).fetchone()
        identity = (
            str(workspace[0]),
            str(workspace[1]).lower(),
            int(workspace[2]),
            str(workspace[3]),
        )
        if existing is not None:
            stored = (
                str(existing[0]),
                str(existing[1]).lower(),
                int(existing[2]),
                str(existing[3]),
            )
            if stored != identity:
                raise BridgeError(
                    "journal_conflict",
                    "Workspace lifecycle identity conflicts with finalized workspace",
                )
            if str(existing[5]) == WorkspaceLifecycleState.REMOVED.value:
                raise BridgeError(
                    "journal_conflict",
                    "Finalized session workspace is already removed",
                )
            if (
                str(existing[4]) == WorkspaceDisposition.PRESERVE.value
                and str(existing[5]) == WorkspaceLifecycleState.PRESERVED.value
            ):
                return
            self.journal._connection.execute(
                """UPDATE workspace_lifecycle SET disposition='preserve',state='preserved',
                requested_at=NULL,started_at=NULL,completed_at=NULL,last_error=NULL,updated_at=?
                WHERE session_id=?""",
                (now, session_id),
            )
        else:
            self.journal._connection.execute(
                """INSERT INTO workspace_lifecycle(session_id,workspace_path,base_sha,expected_revision,
                expected_state_hash,disposition,state,requested_at,started_at,completed_at,last_error,created_at,updated_at)
                VALUES(?,?,?,?,?,'preserve','preserved',NULL,NULL,NULL,NULL,?,?)""",
                (session_id, *identity, now, now),
            )
        self.journal._append_event_in_transaction(
            session_id=session_id,
            event_type="workspace.preserved",
            payload={"state": "preserved"},
            created_at=now,
        )
