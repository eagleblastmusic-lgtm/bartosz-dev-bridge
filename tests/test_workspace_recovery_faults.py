from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from bdb_bridge import BridgeConfig, BridgeError, BridgeErrorCode, CommandState, ExecutionCoordinator, Journal, SessionState, WorkspaceManager
from bdb_bridge.execution import SystemCrash
SESSION = '018f3f66-6cb3-4f66-9f2e-3d7647d1b701'
COMMAND = f'{SESSION}:000001'
NOW = '2026-07-15T12:00:00Z'

def run_git(repo: Path, *args: str) -> str:
    cp = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True, check=False)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()

def init_fixture(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    fixture = root / 'fixture'
    shutil.copytree(
        Path(__file__).parents[1] / 'bdb-poc-fixture',
        fixture,
        ignore=shutil.ignore_patterns('.pytest_cache', '__pycache__', '*.pyc'),
    )
    run_git(fixture, 'init', '-b', 'main')
    run_git(fixture, 'config', 'core.autocrlf', 'false')
    run_git(fixture, 'config', 'user.name', 'Test')
    run_git(fixture, 'config', 'user.email', 'test@example.invalid')
    run_git(fixture, 'add', '--', '.gitattributes', '.gitignore', 'pyproject.toml', 'src', 'tests')
    run_git(fixture, 'commit', '-m', 'baseline')
    return fixture, run_git(fixture, 'rev-parse', 'HEAD')

def config(root: Path, fixture: Path) -> BridgeConfig:
    return BridgeConfig(root / 'control', fixture, root / 'worktrees', allowed_paths=('src/clamp.py', 'tests/test_clamp.py'), python_executable=sys.executable, test_timeout_seconds=20)

def command_document(*, profile: str | None='poc_pytest', old: str='return value', new: str='return max(0, min(100, value))') -> dict:
    payload = {'path': 'src/clamp.py', 'old': old, 'new': new}
    if profile is not None:
        payload['profile_id'] = profile
    return {'schema_version': '1.1', 'session_id': SESSION, 'command_id': COMMAND, 'sequence': 1, 'operation': 'replace_exact_and_test', 'expected_revision': 0, 'payload': payload}

def setup(root: Path, *, document: dict | None=None) -> tuple[BridgeConfig, Path, str]:
    fixture, base = init_fixture(root)
    db = root / 'journal.db'
    journal = Journal.open(db, now_fn=lambda: NOW)
    doc = document or command_document()
    manifest = {'schema_version': '1.1', 'session_id': SESSION, 'repository_id': 'repo', 'base_sha': base, 'allowed_paths': ['src/clamp.py', 'tests/test_clamp.py']}
    journal._connection.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)', (SESSION, 'repo', base, 'active', NOW, NOW))
    journal._connection.execute('INSERT INTO session_ingestion VALUES(?,?,?,?,?,?,?,?,?,?)', (SESSION, 'manifest.json', 'm' * 40, 'raw', 'manifest', json.dumps(manifest), NOW, NOW, NOW, NOW))
    journal._connection.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?,?)', (COMMAND, SESSION, 1, 'cmd', json.dumps(doc), 'c' * 40, 'claimed', 0, None, NOW, NOW))
    journal.close()
    return config(root, fixture), db, base

def counts(j: Journal) -> tuple[int, int, int, int]:
    return (j._connection.execute('SELECT COUNT(*) FROM operation_plans').fetchone()[0], j._connection.execute('SELECT COUNT(*) FROM operation_effects').fetchone()[0], j._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='operation.plan_recorded'").fetchone()[0], j._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='operation.effect_recorded'").fetchone()[0])

def test_ghb04_normal_execute_and_ten_effect_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, db, _ = setup(tmp_path)
    j = Journal.open(db, now_fn=lambda: NOW)
    outcome = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert outcome.status == 'success'
    assert outcome.workspace_revision_after == 1
    assert j.get_command(COMMAND).state == CommandState.EFFECT_RECORDED
    assert counts(j) == (1, 1, 1, 1)
    j.close()
    from bdb_bridge.models import ProfileRunOutcome
    monkeypatch.setattr(ExecutionCoordinator, '_run_profile', lambda self, wm, profile_id='poc_pytest': ProfileRunOutcome('success', 0, '', '', 1))
    for _ in range(10):
        j = Journal.open(db, now_fn=lambda: NOW)
        replay = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
        assert replay.workspace_revision_after == 1
        assert counts(j) == (1, 1, 1, 1)
        j.close()

@pytest.mark.parametrize('point', ['AFTER_PLAN_COMMIT_BEFORE_WRITE', 'AFTER_TEMP_WRITE_BEFORE_REPLACE', 'AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT', 'AFTER_EFFECT_COMMIT_BEFORE_PROFILE', 'BEFORE_PROFILE'])
def test_ghb04_crash_recovery_matrix(tmp_path: Path, point: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from bdb_bridge.models import ProfileRunOutcome
    monkeypatch.setattr(ExecutionCoordinator, '_run_profile', lambda self, wm, profile_id='poc_pytest': ProfileRunOutcome('success', 0, '3 passed', '', 1))
    root = tmp_path / point.lower()
    cfg, db, base = setup(root)
    j = Journal.open(db, now_fn=lambda: NOW)
    def hook(actual: str) -> None:
        if actual == point:
            raise SystemCrash(point)
    with pytest.raises(SystemCrash):
        ExecutionCoordinator(cfg, j, hook).execute_or_recover(COMMAND)
    j.close()
    j = Journal.open(db, now_fn=lambda: NOW)
    result = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert result.status == 'success'
    assert result.workspace_revision_after == 1
    assert counts(j) == (1, 1, 1, 1)
    assert j.get_command(COMMAND).state == CommandState.EFFECT_RECORDED
    assert run_git(cfg.fixture_repo_path, 'status', '--porcelain=v1') == ''
    target = cfg.worktree_root / SESSION / 'src' / 'clamp.py'
    assert target.read_text(encoding='utf-8').count('return max(0, min(100, value))') == 1
    j.close()

def test_ghb04_preplan_foreign_file_blocks_without_plan_or_patch(tmp_path: Path) -> None:
    cfg, db, base = setup(tmp_path)
    j = Journal.open(db, now_fn=lambda: NOW)
    wm = WorkspaceManager(cfg, SESSION, base, ['src/clamp.py', 'tests/test_clamp.py'])
    wm.ensure_workspace(j)
    foreign = cfg.worktree_root / SESSION / 'src' / 'untracked.py'
    foreign.write_bytes(b'foreign')
    result = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert result.status == 'manual_reconciliation_required'
    assert foreign.read_bytes() == b'foreign'
    assert counts(j)[:2] == (0, 0)
    assert j.get_workspace(SESSION).revision == 0
    assert j.get_command(COMMAND).state == CommandState.MANUAL_RECONCILIATION_REQUIRED
    assert j.get_session(SESSION).state == SessionState.MANUAL_RECONCILIATION_REQUIRED
    assert j._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='workspace.recovery_blocked'").fetchone()[0] == 1
    for _ in range(10):
        ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert j._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='workspace.recovery_blocked'").fetchone()[0] == 1
    assert foreign.exists()
    j.close()

def test_ghb04_manual_target_divergence_replay_is_atomic(tmp_path: Path) -> None:
    cfg, db, base = setup(tmp_path)
    j = Journal.open(db, now_fn=lambda: NOW)
    def crash(point: str) -> None:
        if point == 'AFTER_PLAN_COMMIT_BEFORE_WRITE':
            raise SystemCrash()
    with pytest.raises(SystemCrash):
        ExecutionCoordinator(cfg, j, crash).execute_or_recover(COMMAND)
    j.close()
    target = cfg.worktree_root / SESSION / 'src' / 'clamp.py'
    target.write_bytes(b'manual foreign bytes')
    j = Journal.open(db, now_fn=lambda: NOW)
    result = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert result.status == 'manual_reconciliation_required'
    assert target.read_bytes() == b'manual foreign bytes'
    for _ in range(10):
        ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert j._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='workspace.recovery_blocked'").fetchone()[0] == 1
    assert j.get_workspace(SESSION).revision == 0
    assert j.get_operation_effect(COMMAND) is None
    j.close()

def test_ghb04_manual_transaction_faults_roll_back(tmp_path: Path) -> None:
    for point in ('AFTER_MANUAL_COMMAND_TRANSITION', 'BEFORE_MANUAL_EVENT'):
        root = tmp_path / point
        cfg, db, _ = setup(root)
        j = Journal.open(db, now_fn=lambda: NOW)
        def hook(actual: str) -> None:
            if actual == point:
                raise RuntimeError(point)
        with pytest.raises(RuntimeError):
            j.mark_workspace_recovery_blocked(session_id=SESSION, command_id=COMMAND, reason_code='x', diagnostic={}, fault_hook=hook)
        assert j.get_command(COMMAND).state == CommandState.CLAIMED
        assert j.get_session(SESSION).state == SessionState.ACTIVE
        assert j._connection.execute("SELECT COUNT(*) FROM events WHERE event_type='workspace.recovery_blocked'").fetchone()[0] == 0
        j.close()

@pytest.mark.parametrize('profile', [None, 'arbitrary'])
def test_ghb04_profile_missing_or_arbitrary_is_denied_before_plan(tmp_path: Path, profile: str | None) -> None:
    cfg, db, _ = setup(tmp_path, document=command_document(profile=profile))
    j = Journal.open(db, now_fn=lambda: NOW)
    result = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert result.status == 'policy_denied'
    assert counts(j)[:2] == (0, 0)
    assert j.get_workspace(SESSION).revision == 0
    assert j.get_command(COMMAND).state == CommandState.POLICY_DENIED
    j.close()

def test_ghb04_stale_revision_and_state_mismatch_from_claimed(tmp_path: Path) -> None:
    root = tmp_path / 'stale'
    cfg, db, _ = setup(root)
    j = Journal.open(db, now_fn=lambda: NOW)
    j._connection.execute('UPDATE commands SET expected_revision=1 WHERE command_id=?', (COMMAND,))
    result = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert result.status == 'stale_revision' and j.get_command(COMMAND).state == CommandState.STALE_REVISION
    assert counts(j)[:2] == (0, 0)
    j.close()
    root = tmp_path / 'state'
    cfg, db, _ = setup(root)
    j = Journal.open(db, now_fn=lambda: NOW)
    j._connection.execute("UPDATE commands SET expected_state_hash='sha256:bad' WHERE command_id=?", (COMMAND,))
    result = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert result.status == 'state_mismatch' and j.get_command(COMMAND).state == CommandState.STATE_MISMATCH
    assert counts(j)[:2] == (0, 0)
    j.close()

@pytest.mark.parametrize('path', ['src/not-allowed.py', 'tests/test_clamp.py'])
def test_ghb04_path_policy_and_manifest_scope_before_plan(tmp_path: Path, path: str) -> None:
    doc = command_document()
    doc['payload']['path'] = path
    cfg, db, _ = setup(tmp_path, document=doc)
    if path == 'tests/test_clamp.py':
        j = Journal.open(db, now_fn=lambda: NOW)
        manifest = json.loads(j._connection.execute('SELECT manifest_json FROM session_ingestion WHERE session_id=?', (SESSION,)).fetchone()[0])
        manifest['allowed_paths'] = ['src/clamp.py']
        j._connection.execute('UPDATE session_ingestion SET manifest_json=? WHERE session_id=?', (json.dumps(manifest), SESSION))
        j.close()
    j = Journal.open(db, now_fn=lambda: NOW)
    result = ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert result.status == 'policy_denied' and counts(j)[:2] == (0, 0)
    j.close()

def test_ghb04_invalid_utf8_and_replace_mismatch_do_not_record_plan(tmp_path: Path) -> None:
    root = tmp_path / 'utf8'
    cfg, db, base = setup(root)
    j = Journal.open(db, now_fn=lambda: NOW)
    wm = WorkspaceManager(cfg, SESSION, base, ['src/clamp.py', 'tests/test_clamp.py'])
    wm.ensure_workspace(j)
    target = cfg.worktree_root / SESSION / 'src' / 'clamp.py'
    target.write_bytes(b'\xff\xfe')
    actual = wm.compute_state_hash()
    j._connection.execute('UPDATE workspaces SET state_hash=? WHERE session_id=?', (actual, SESSION))
    with pytest.raises(BridgeError) as exc:
        ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
    assert exc.value.code == BridgeErrorCode.INVALID_PAYLOAD and counts(j)[:2] == (0, 0)
    j.close()
    for name, old in (('zero', 'does not exist'), ('many', 'a')):
        root = tmp_path / name
        cfg, db, _ = setup(root, document=command_document(old=old))
        j = Journal.open(db, now_fn=lambda: NOW)
        with pytest.raises(BridgeError) as exc:
            ExecutionCoordinator(cfg, j).execute_or_recover(COMMAND)
        assert exc.value.code == BridgeErrorCode.REPLACE_MISMATCH and counts(j)[:2] == (0, 0)
        j.close()
