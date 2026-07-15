from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from bdb_bridge import (
    CommandState,
    ExecutionOutcome,
    Journal,
    OperationEffectRecord,
    OperationPlanRecord,
    ProfileRunOutcome,
    ResultBuildInput,
    ResultStager,
    SessionState,
    compute_operation_effect_sha256,
    compute_operation_plan_sha256,
    sha256_bytes,
)

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
COMMAND_ID = f"{SESSION_ID}:000001"
BASE_SHA = "a" * 40
COMMAND_COMMIT_SHA = "b" * 40
NOW = "2026-07-15T12:00:00Z"
FINISHED = "2026-07-15T12:00:02Z"
BEFORE = b"value = 1\n"
AFTER = b"value = 2\n"
STATE_BEFORE = "sha256:" + "1" * 64
STATE_AFTER = "sha256:" + "2" * 64


def fixed_now() -> str:
    return NOW


def make_journal(tmp_path: Path, *, state: CommandState = CommandState.EFFECT_RECORDED) -> Journal:
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    command_json = json.dumps(
        {
            "schema_version": "1.1",
            "session_id": SESSION_ID,
            "command_id": COMMAND_ID,
            "sequence": 1,
            "operation": "replace_exact_and_test",
            "expected_revision": 0,
            "expected_state_hash": STATE_BEFORE,
            "payload": {
                "path": "src/clamp.py",
                "old": "value = 1",
                "new": "value = 2",
                "profile_id": "poc_pytest",
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    journal._connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "fixture", BASE_SHA, SessionState.ACTIVE.value, NOW, NOW),
    )
    journal._connection.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            COMMAND_ID,
            SESSION_ID,
            1,
            "sha256:" + "c" * 64,
            command_json,
            COMMAND_COMMIT_SHA,
            state.value,
            0,
            STATE_BEFORE,
            NOW,
            NOW,
        ),
    )
    journal._connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SESSION_ID, str(tmp_path / "workspace"), BASE_SHA, 1, STATE_AFTER, NOW, NOW),
    )
    candidate_plan = OperationPlanRecord(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        operation="replace_exact_and_test",
        target_path="src/clamp.py",
        profile_id="poc_pytest",
        expected_revision=0,
        expected_state_hash=STATE_BEFORE,
        workspace_revision_before=0,
        workspace_state_hash_before=STATE_BEFORE,
        before_content=BEFORE,
        before_content_sha256=sha256_bytes(BEFORE),
        planned_after_content=AFTER,
        planned_after_content_sha256=sha256_bytes(AFTER),
        planned_after_state_hash=STATE_AFTER,
        plan_sha256="",
        created_at=NOW,
    )
    plan = replace(candidate_plan, plan_sha256=compute_operation_plan_sha256(candidate_plan))
    candidate_effect = OperationEffectRecord(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        plan_sha256=plan.plan_sha256,
        target_path=plan.target_path,
        workspace_revision_before=0,
        workspace_revision_after=1,
        workspace_state_hash_before=STATE_BEFORE,
        workspace_state_hash_after=STATE_AFTER,
        before_content_sha256=plan.before_content_sha256,
        after_content_sha256=plan.planned_after_content_sha256,
        effect_sha256="",
        recorded_at=NOW,
    )
    effect = replace(candidate_effect, effect_sha256=compute_operation_effect_sha256(candidate_effect))
    journal._connection.execute(
        """INSERT INTO operation_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan.command_id, plan.session_id, plan.operation, plan.target_path, plan.profile_id,
            plan.expected_revision, plan.expected_state_hash, plan.workspace_revision_before,
            plan.workspace_state_hash_before, plan.before_content, plan.before_content_sha256,
            plan.planned_after_content, plan.planned_after_content_sha256,
            plan.planned_after_state_hash, plan.plan_sha256, plan.created_at,
        ),
    )
    journal._connection.execute(
        """INSERT INTO operation_effects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            effect.command_id, effect.session_id, effect.plan_sha256, effect.target_path,
            effect.workspace_revision_before, effect.workspace_revision_after,
            effect.workspace_state_hash_before, effect.workspace_state_hash_after,
            effect.before_content_sha256, effect.after_content_sha256,
            effect.effect_sha256, effect.recorded_at,
        ),
    )
    return journal


def make_outcome(*, status: str = "success", stdout: str = "3 passed\n", stderr: str = "") -> ExecutionOutcome:
    return ExecutionOutcome(
        status=status,
        error_code=None if status == "success" else status,
        summary="Command effect recorded",
        workspace_revision_before=0,
        workspace_revision_after=1,
        workspace_state_hash_before=STATE_BEFORE,
        workspace_state_hash_after=STATE_AFTER,
        changed_files=["src/clamp.py"],
        diff="diff --git a/src/clamp.py b/src/clamp.py\n-value = 1\n+value = 2\n",
        profile_run=ProfileRunOutcome(
            status=status,
            exit_code=0 if status == "success" else 1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=10,
        ),
    )


def build_staged(journal: Journal, *, status: str = "success", stdout: str = "3 passed\n"):
    session = journal.get_session(SESSION_ID)
    command = journal.get_command(COMMAND_ID)
    plan = journal.get_operation_plan(COMMAND_ID)
    effect = journal.get_operation_effect(COMMAND_ID)
    assert session and command and plan and effect
    return ResultStager(journal).build(
        ResultBuildInput(
            session=session,
            command=command,
            plan=plan,
            effect=effect,
            outcome=make_outcome(status=status, stdout=stdout),
            started_at=NOW,
            finished_at=FINISHED,
        )
    )


def stage(journal: Journal):
    staged = build_staged(journal)
    result, outbox = journal.stage_result_and_enqueue(
        command_id=COMMAND_ID,
        result_json=staged.result_json,
        remote_path=staged.remote_path,
    )
    return staged, result, outbox
