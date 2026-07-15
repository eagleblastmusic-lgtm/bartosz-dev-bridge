from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    BridgeErrorCode,
    CommandState,
    Journal,
    OperationEffectRecord,
    OperationPlanRecord,
    compute_operation_effect_sha256,
    compute_operation_plan_sha256,
    sha256_bytes,
)

SESSION_ID = "018f3f66-6cb3-4f66-9f2e-3d7647d1b701"
COMMAND_ID = f"{SESSION_ID}:000001"
FIXED_NOW = "2026-07-15T12:00:00Z"


def fixed_now() -> str:
    return FIXED_NOW


def setup_records(tmp_path: Path) -> Journal:
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    journal._connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "repo1", "a" * 40, "active", FIXED_NOW, FIXED_NOW),
    )
    journal._connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SESSION_ID, "/path/to/ws", "a" * 40, 0, "before", FIXED_NOW, FIXED_NOW),
    )
    journal._connection.execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (COMMAND_ID, SESSION_ID, 1, "cmd", "{}", None, "claimed", 0, None, FIXED_NOW, FIXED_NOW),
    )
    return journal


def sample_plan() -> OperationPlanRecord:
    before = b"\xef\xbb\xbfA\r\n\xc4\x85\n"
    after = b"B\r\n"
    candidate = OperationPlanRecord(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        operation="replace_exact_and_test",
        target_path="src/clamp.py",
        profile_id="poc_pytest",
        expected_revision=0,
        expected_state_hash=None,
        workspace_revision_before=0,
        workspace_state_hash_before="before",
        before_content=before,
        before_content_sha256=sha256_bytes(before),
        planned_after_content=after,
        planned_after_content_sha256=sha256_bytes(after),
        planned_after_state_hash="after",
        plan_sha256="",
        created_at=FIXED_NOW,
    )
    return replace(candidate, plan_sha256=compute_operation_plan_sha256(candidate))


def sample_effect(plan: OperationPlanRecord) -> OperationEffectRecord:
    candidate = OperationEffectRecord(
        command_id=plan.command_id,
        session_id=plan.session_id,
        plan_sha256=plan.plan_sha256,
        target_path=plan.target_path,
        workspace_revision_before=0,
        workspace_revision_after=1,
        workspace_state_hash_before="before",
        workspace_state_hash_after="after",
        before_content_sha256=plan.before_content_sha256,
        after_content_sha256=plan.planned_after_content_sha256,
        effect_sha256="",
        recorded_at=FIXED_NOW,
    )
    return replace(candidate, effect_sha256=compute_operation_effect_sha256(candidate))


def test_ghb04_plan_effect_ten_replays_are_one_record(tmp_path: Path) -> None:
    journal = setup_records(tmp_path)
    plan = sample_plan()
    effect = sample_effect(plan)
    journal.record_operation_plan(plan)
    journal.record_operation_effect(effect)
    for _ in range(10):
        journal.record_operation_plan(plan)
        journal.record_operation_effect(effect)
    assert journal._connection.execute("SELECT COUNT(*) FROM operation_plans").fetchone()[0] == 1
    assert journal._connection.execute("SELECT COUNT(*) FROM operation_effects").fetchone()[0] == 1
    assert journal._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='operation.plan_recorded'").fetchone()[0] == 1
    assert journal._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='operation.effect_recorded'").fetchone()[0] == 1
    assert journal.get_workspace(SESSION_ID).revision == 1
    assert journal.get_command(COMMAND_ID).state == CommandState.EFFECT_RECORDED
    journal.close()


def test_ghb04_plan_collision_checks_all_immutable_fields(tmp_path: Path) -> None:
    journal = setup_records(tmp_path)
    plan = sample_plan()
    journal.record_operation_plan(plan)
    attack = replace(plan, target_path="src/other.py", plan_sha256=plan.plan_sha256)
    with pytest.raises(BridgeError) as exc:
        journal.record_operation_plan(attack)
    assert exc.value.code == BridgeErrorCode.OPERATION_PLAN_COLLISION
    journal.close()


def test_ghb04_effect_collision_checks_all_immutable_fields(tmp_path: Path) -> None:
    journal = setup_records(tmp_path)
    plan = sample_plan()
    journal.record_operation_plan(plan)
    effect = sample_effect(plan)
    journal.record_operation_effect(effect)
    attack = replace(effect, target_path="src/other.py", effect_sha256=effect.effect_sha256)
    with pytest.raises(BridgeError) as exc:
        journal.record_operation_effect(attack)
    assert exc.value.code == BridgeErrorCode.EFFECT_COLLISION
    journal.close()


@pytest.mark.parametrize("point", ["AFTER_WORKSPACE_CAS", "AFTER_EFFECT_INSERT", "BEFORE_EFFECT_EVENT"])
def test_ghb04_effect_transaction_fault_rolls_back(tmp_path: Path, point: str) -> None:
    journal = setup_records(tmp_path)
    plan = sample_plan()
    journal.record_operation_plan(plan)
    effect = sample_effect(plan)

    def hook(actual: str) -> None:
        if actual == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError):
        journal.record_operation_effect(effect, fault_hook=hook)
    assert journal.get_workspace(SESSION_ID).revision == 0
    assert journal.get_workspace(SESSION_ID).state_hash == "before"
    assert journal.get_command(COMMAND_ID).state == CommandState.EXECUTING
    assert journal.get_operation_effect(COMMAND_ID) is None
    assert journal._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='operation.effect_recorded'").fetchone()[0] == 0
    journal.close()


def test_ghb04_exact_bytes_persist_bit_for_bit_after_reopen(tmp_path: Path) -> None:
    journal = setup_records(tmp_path)
    plan = sample_plan()
    journal.record_operation_plan(plan)
    journal.close()
    journal = Journal.open(tmp_path / "journal.db", now_fn=fixed_now)
    stored = journal.get_operation_plan(COMMAND_ID)
    assert stored is not None
    assert stored.before_content == b"\xef\xbb\xbfA\r\n\xc4\x85\n"
    assert stored.planned_after_content == b"B\r\n"
    assert stored.before_content_sha256 == sha256_bytes(stored.before_content)
    assert stored.plan_sha256 == compute_operation_plan_sha256(stored)
    journal.close()
