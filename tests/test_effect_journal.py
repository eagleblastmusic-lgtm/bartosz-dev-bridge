from __future__ import annotations

import json
from pathlib import Path
import pytest

from bdb_bridge import Journal, BridgeError, BridgeErrorCode, OperationPlanRecord, OperationEffectRecord, CommandState

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
COMMAND_ID = f"{SESSION_ID}:000001"
FIXED_NOW = "2026-07-15T12:00:00Z"
def fixed_now() -> str:
    return FIXED_NOW

def setup_session_and_claimed_command(journal: Journal) -> None:
    now = FIXED_NOW
    # Record session
    journal._connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "repo1", "a" * 40, "active", now, now)
    )
    # Record workspace
    journal._connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "/path/to/ws", "a" * 40, 0, "hashws", now, now)
    )
    # Record command in CLAIMED state
    journal._connection.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (COMMAND_ID, SESSION_ID, 1, "sha256:" + "a" * 64, "{}", "c" * 40, CommandState.CLAIMED.value, 0, "hashws", now, now)
    )

def make_sample_plan() -> OperationPlanRecord:
    return OperationPlanRecord(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        operation="replace_exact_and_test",
        target_path="src/clamp.py",
        profile_id="poc_pytest",
        expected_revision=0,
        expected_state_hash="hashws",
        workspace_revision_before=0,
        workspace_state_hash_before="hashws",
        before_content=b"old text",
        before_content_sha256="hash_old",
        planned_after_content=b"new text",
        planned_after_content_sha256="hash_new",
        planned_after_state_hash="hash_after",
        plan_sha256="plan_sha",
        created_at=FIXED_NOW,
    )

def make_sample_effect() -> OperationEffectRecord:
    return OperationEffectRecord(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        plan_sha256="plan_sha",
        target_path="src/clamp.py",
        workspace_revision_before=0,
        workspace_revision_after=1,
        workspace_state_hash_before="hashws",
        workspace_state_hash_after="hash_after",
        before_content_sha256="hash_old",
        after_content_sha256="hash_new",
        effect_sha256="effect_sha",
        recorded_at=FIXED_NOW,
    )

def test_record_and_get_operation_plan(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    setup_session_and_claimed_command(journal)

    plan = make_sample_plan()

    # 1. First record plan
    journal.record_operation_plan(plan)

    # Check command transitioned CLAIMED -> EXECUTING
    cmd = journal.get_command(COMMAND_ID)
    assert cmd is not None
    assert cmd.state == CommandState.EXECUTING

    # Get plan and verify
    retrieved = journal.get_operation_plan(COMMAND_ID)
    assert retrieved == plan

    # Verify exact one event operation.plan_recorded
    events = [e for e in journal.list_events() if e.event_type == "operation.plan_recorded"]
    assert len(events) == 1
    assert events[0].command_id == COMMAND_ID

    # 2. Identical replay is no-op
    journal.record_operation_plan(plan)

    events2 = [e for e in journal.list_events() if e.event_type == "operation.plan_recorded"]
    assert len(events2) == 1  # No duplicate event

    # 3. Collision with different plan raises collision error
    different_plan = make_sample_plan()
    object.__setattr__(different_plan, "plan_sha256", "different_sha")
    with pytest.raises(BridgeError) as exc:
        journal.record_operation_plan(different_plan)
    assert exc.value.code == BridgeErrorCode.OPERATION_PLAN_COLLISION

    journal.close()

def test_record_and_get_operation_effect(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    setup_session_and_claimed_command(journal)

    plan = make_sample_plan()
    journal.record_operation_plan(plan)

    effect = make_sample_effect()

    # 1. Record effect (performs CAS on workspace and transitions command)
    journal.record_operation_effect(effect)

    # Verify command state EXECUTING -> EFFECT_RECORDED
    cmd = journal.get_command(COMMAND_ID)
    assert cmd is not None
    assert cmd.state == CommandState.EFFECT_RECORDED

    # Verify workspace record has been advanced (revision=1, state_hash=hash_after)
    ws = journal.get_workspace(SESSION_ID)
    assert ws is not None
    assert ws.revision == 1
    assert ws.state_hash == "hash_after"

    # Verify get_operation_effect
    retrieved = journal.get_operation_effect(COMMAND_ID)
    assert retrieved == effect

    # Verify exact one event operation.effect_recorded
    events = [e for e in journal.list_events() if e.event_type == "operation.effect_recorded"]
    assert len(events) == 1

    # 2. Identical effect replay is no-op
    # Re-run requires command to be EXECUTING. In record_operation_effect,
    # if the effect already exists, it checks if the effect_sha256 is the same.
    # If yes, it returns immediately without executing checks or raising transition errors!
    journal.record_operation_effect(effect)

    events2 = [e for e in journal.list_events() if e.event_type == "operation.effect_recorded"]
    assert len(events2) == 1  # No duplicate event

    # Verify workspace revision has increased exactly once
    ws2 = journal.get_workspace(SESSION_ID)
    assert ws2.revision == 1

    # 3. Collision with different effect raises error
    different_effect = make_sample_effect()
    object.__setattr__(different_effect, "effect_sha256", "different_effect_sha")
    with pytest.raises(BridgeError) as exc:
        journal.record_operation_effect(different_effect)
    assert exc.value.code == BridgeErrorCode.EFFECT_COLLISION

    journal.close()

def test_10_replays_remain_one_record(tmp_path: Path) -> None:
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    setup_session_and_claimed_command(journal)

    plan = make_sample_plan()
    effect = make_sample_effect()

    journal.record_operation_plan(plan)
    journal.record_operation_effect(effect)

    for _ in range(10):
        journal.record_operation_plan(plan)
        journal.record_operation_effect(effect)

    # Check DB table counts
    assert journal._connection.execute("SELECT COUNT(*) FROM operation_plans").fetchone()[0] == 1
    assert journal._connection.execute("SELECT COUNT(*) FROM operation_effects").fetchone()[0] == 1

    # Check event counts
    assert len([e for e in journal.list_events() if e.event_type == "operation.plan_recorded"]) == 1
    assert len([e for e in journal.list_events() if e.event_type == "operation.effect_recorded"]) == 1

    # Check revision remains 1
    ws = journal.get_workspace(SESSION_ID)
    assert ws.revision == 1

    journal.close()
