from __future__ import annotations

import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import bdb_vnext.project_history_reconciliation as recovery
from bdb_vnext.project_history_reconciliation import (
    AcceptedTaskCommit,
    ProjectHistoryReconciliationError,
    ProjectHistoryReconciler,
    parse_accepted_task_commits,
    select_contiguous_reconciliation,
)


@dataclass(frozen=True)
class _State:
    revision: int
    execution: dict
    events: tuple[dict, ...] = ()


class _Memory:
    def __init__(self, plan, state: _State) -> None:
        self.plan = plan
        self.state = state

    def current_plan(self):
        return self.plan

    def read_state(self):
        return self.state

    def _append_event(self, state, event_type, summary, **fields):
        event = {"event_type": event_type, "summary": summary, **fields}
        return replace(state, events=state.events + (event,))

    def execution_transaction(self, operation, *, expected_revision=None):
        if expected_revision is not None and expected_revision != self.state.revision:
            raise AssertionError("stale test revision")
        updated, result = operation(self.state)
        self.state = replace(updated, revision=self.state.revision + 1)
        return result


@dataclass(frozen=True)
class _Task:
    task_id: str
    milestone_id: str
    status: str = "pending"


@dataclass(frozen=True)
class _Milestone:
    milestone_id: str


def _plan():
    return SimpleNamespace(
        plan_version="1",
        tasks=(
            _Task("P0-01", "P0"),
            _Task("P1-01", "P1"),
            _Task("P1-02", "P1"),
            _Task("P2-01", "P2"),
        ),
        milestones=(_Milestone("P0"), _Milestone("P1"), _Milestone("P2")),
    )


def _accepted_record(sha: str, task_id: str, *, body: str | None = None) -> str:
    return (
        f"{sha}\x1f[skip ci] feat: complete {task_id} domain work\x1f"
        f"{body or f'{task_id} accepted: exact validation passed.'}\x1e"
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _commit(repo: Path, task_id: str, counter: int) -> str:
    path = repo / f"{task_id}.txt"
    path.write_text(str(counter), encoding="utf-8")
    _git(repo, "add", path.name)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "-m",
            f"[skip ci] feat: complete {task_id} test work",
            "-m",
            f"{task_id} accepted: exact-toolchain validation passed.",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def test_parser_requires_both_complete_subject_and_accepted_body() -> None:
    good = "a" * 40
    bad_body = "b" * 40
    wrong_subject = "c" * 40
    text = (
        _accepted_record(good, "P1-01")
        + _accepted_record(bad_body, "P1-02", body="validation passed without canonical acceptance marker")
        + f"{wrong_subject}\x1f[skip ci] feat: implement P1-03\x1fP1-03 accepted: pass.\x1e"
    )

    found = parse_accepted_task_commits(text, ("P1-01", "P1-02", "P1-03"))

    assert [(item.task_id, item.commit_sha) for item in found] == [("P1-01", good)]


def test_parser_accepts_milestone_run_wording_and_rejects_duplicate_task() -> None:
    first = "1" * 40
    text = _accepted_record(first, "P2-06", body="P2-06 accepted in milestone run run-123. Full gate passed.")
    found = parse_accepted_task_commits(text, ("P2-06",))
    assert found[0].task_id == "P2-06"

    duplicate = text + _accepted_record("2" * 40, "P2-06")
    with pytest.raises(ProjectHistoryReconciliationError, match="multiple accepted commits"):
        parse_accepted_task_commits(duplicate, ("P2-06",))


def test_contiguous_selection_stops_at_first_unevidenced_gap() -> None:
    plan = _plan()
    execution = {"task_statuses": {"P0-01": "completed", "P1-01": "blocked"}}
    evidence = (
        AcceptedTaskCommit("P1-01", "1" * 40, "one"),
        AcceptedTaskCommit("P2-01", "2" * 40, "two"),
    )

    importable, already, ignored, gap = select_contiguous_reconciliation(plan, execution, evidence)

    assert tuple(item.task_id for item in importable) == ("P1-01",)
    assert already == ("P0-01",)
    assert ignored == ("P2-01",)
    assert gap == "P1-02"


def test_real_git_history_reconciles_blocked_prefix_without_fabricating_acceptance_results(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "BDB Test")
    base = repo / "base.txt"
    base.write_text("base", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "bootstrap")
    sha1 = _commit(repo, "P1-01", 1)
    sha2 = _commit(repo, "P1-02", 2)

    plan = _plan()
    memory = _Memory(
        plan,
        _State(
            revision=7,
            execution={
                "task_statuses": {"P0-01": "completed", "P1-01": "blocked", "P1-02": "pending", "P2-01": "pending"},
                "bindings": [
                    {
                        "task_id": "P1-01",
                        "status": "FAILED",
                        "superseded": False,
                        "execution_binding_id": "binding-old",
                    }
                ],
                "current_task_id": "P1-01",
                "current_binding_id": None,
                "acceptance_results": [],
                "milestones_completed": ["P0"],
            },
        ),
    )
    project = SimpleNamespace(
        project_id="project-1",
        display_name="Demo",
        repo_alias="demo",
        local_repo_path=str(repo),
        github_repo=None,
    )

    class _Catalog:
        def __init__(self, _runtime_root):
            pass

        def get(self, project_id):
            return project if project_id == "project-1" else None

    monkeypatch.setattr(recovery, "ProjectCatalog", _Catalog)
    monkeypatch.setattr(recovery, "ProjectMemoryStore", lambda _root, _project_id: memory)

    reconciler = ProjectHistoryReconciler(tmp_path / "runtime", "project-1")
    preview = reconciler.preview(source_ref="main")

    assert [item.task_id for item in preview.importable] == ["P1-01", "P1-02"]
    assert preview.first_unreconciled_task == "P2-01"
    assert preview.needs_alignment is False

    receipt = reconciler.apply(preview)

    assert receipt.imported_task_ids == ("P1-01", "P1-02")
    assert receipt.completed_milestones == ("P1",)
    assert receipt.repo_head_after == sha2
    assert memory.state.execution["task_statuses"]["P1-01"] == "completed"
    assert memory.state.execution["task_statuses"]["P1-02"] == "completed"
    assert memory.state.execution["current_task_id"] is None
    assert memory.state.execution["current_binding_id"] is None
    assert memory.state.execution["acceptance_results"] == []
    assert {event["git_head"] for event in memory.state.events if event["event_type"] == "TASK_COMPLETED"} == {sha1, sha2}

    second = reconciler.preview(source_ref="main")
    assert second.importable == ()
    assert reconciler.apply(second).idempotent is True


def test_apply_fails_closed_when_an_imported_task_has_active_binding(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "BDB Test")
    base = repo / "base.txt"
    base.write_text("base", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "bootstrap")
    _commit(repo, "P1-01", 1)

    plan = _plan()
    memory = _Memory(
        plan,
        _State(
            revision=3,
            execution={
                "task_statuses": {"P0-01": "completed", "P1-01": "blocked"},
                "bindings": [{"task_id": "P1-01", "status": "ACTIVE", "superseded": False}],
                "milestones_completed": ["P0"],
            },
        ),
    )
    project = SimpleNamespace(local_repo_path=str(repo), github_repo=None)

    class _Catalog:
        def __init__(self, _runtime_root):
            pass

        def get(self, project_id):
            return project if project_id == "project-1" else None

    monkeypatch.setattr(recovery, "ProjectCatalog", _Catalog)
    monkeypatch.setattr(recovery, "ProjectMemoryStore", lambda _root, _project_id: memory)

    reconciler = ProjectHistoryReconciler(tmp_path / "runtime", "project-1")
    preview = reconciler.preview(source_ref="main")
    with pytest.raises(ProjectHistoryReconciliationError, match="active execution binding"):
        reconciler.apply(preview)
