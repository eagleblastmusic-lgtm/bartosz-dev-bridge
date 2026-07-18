from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPERATOR = ROOT / "scripts" / "Invoke-BDBWorkspaceLoop.ps1"


def read_operator() -> str:
    return OPERATOR.read_text(encoding="utf-8")


def test_operator_recovers_only_a_proven_safe_stale_bridge() -> None:
    source = read_operator()
    assert "function Test-SafeStaleBridgeState" in source
    assert '$Bridge.status -eq "STALE"' in source
    assert '$Bridge.lock_held -eq $false' in source
    assert '$Bridge.pid_alive -eq $false' in source
    assert "function Start-OrRecoverBridge" in source
    assert source.count("Start-OrRecoverBridge $pythonExecutable $bridgeConfig $bridge") >= 2


def test_stop_restarts_safe_stale_then_uses_graceful_stop() -> None:
    source = read_operator()
    stale_index = source.index("if (Test-SafeStaleBridgeState $bridge)")
    start_index = source.index(
        "$bridge = Start-OrRecoverBridge $pythonExecutable $bridgeConfig $bridge",
        stale_index,
    )
    stop_index = source.index('"-m", "bdb_bridge", "bridge", "stop"', start_index)
    offline_index = source.index(
        '$bridge = Wait-ForBridgeState $pythonExecutable $bridgeConfig "OFFLINE"',
        stop_index,
    )
    assert stale_index < start_index < stop_index < offline_index
    assert 'throw "Bridge cannot stop safely from state $($bridge.status)"' in source


def test_operator_only_removes_a_confirmed_stale_promoter_pid_file() -> None:
    source = read_operator()
    assert "function Remove-StalePromoterPidFile" in source
    assert "Test-ProcessAlive ([int]$status.pid)" in source
    assert "Remove-Item -LiteralPath $pidFile -Force" in source
    forbidden = (
        "Stop-Process",
        "taskkill",
        "TerminateProcess",
        "Remove-Item -Recurse",
        "git clean",
        "reset --hard",
        "worktree prune",
    )
    for token in forbidden:
        assert token not in source
