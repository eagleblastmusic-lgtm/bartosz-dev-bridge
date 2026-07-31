from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEEPER = ROOT / "scripts" / "Invoke-BDBSessionArmKeeper.ps1"
SESSION = ROOT / "scripts" / "Invoke-GicleeAppBDBSession.ps1"


def test_keeper_has_bounded_session_and_idle_timeout() -> None:
    source = KEEPER.read_text(encoding="utf-8")
    assert "[ValidateRange(1, 12)]" in source
    assert "[int]$SessionHours = 12" in source
    assert "[int]$IdleMinutes = 90" in source
    assert '"session_max_reached"' in source
    assert '"idle_timeout"' in source


def test_keeper_uses_short_renewable_lease() -> None:
    source = KEEPER.read_text(encoding="utf-8")
    assert "[int]$LeaseMinutes = 60" in source
    assert "[int]$RenewBeforeMinutes = 10" in source
    assert "bridge native-host arm" in source
    assert "--minutes $LeaseMinutes" in source
    assert "armed_until" in source


def test_keeper_is_tied_to_safe_local_workspace_state() -> None:
    source = KEEPER.read_text(encoding="utf-8")
    assert 'alias -ne "gicleeapp"' in source
    assert "$loop.controlled_clean -ne $true" in source
    assert "$loop.bridge.pid_alive -ne $true" in source
    assert "$loop.bridge.lock_held -ne $true" in source
    assert '"bridge_instance_changed"' in source
    assert '"native_config_changed"' in source
    assert "Get-FileHash -LiteralPath $nativeConfig" in source


def test_keeper_has_cooperative_stop_and_owned_disarm() -> None:
    source = KEEPER.read_text(encoding="utf-8")
    assert "keeper.stop" in source
    assert "Invoke-DisarmIfOwned" in source
    assert "bridge native-host disarm" in source
    assert "[string]$native.generation_id -eq $OwnedGenerationId" in source
    assert "FileShare]::None" in source


def test_session_launcher_stays_local_and_assisted() -> None:
    source = SESSION.read_text(encoding="utf-8")
    assert '"local_assisted"' in source
    assert "auto_mode = $false" in source
    assert "github_transport = $false" in source
    assert "github_push = $false" in source
    assert "shopify_operation = $false" in source
    assert "shopify_publish = $false" in source
    assert "Invoke-BDBWorkspaceLoop.ps1" in source
    assert "Invoke-BDBSessionArmKeeper.ps1" in source
    assert "gh " not in source
    assert "git push" not in source

def test_keeper_parses_json_dates_without_current_culture_roundtrip() -> None:
    source = KEEPER.read_text(encoding="utf-8")
    assert "function ConvertTo-UtcDateTimeOffset" in source
    assert "$Value -is [DateTimeOffset]" in source
    assert "$Value -is [DateTime]" in source
    assert "[Globalization.CultureInfo]::InvariantCulture" in source
    assert "[Globalization.DateTimeStyles]::AdjustToUniversal" in source
    assert "ConvertTo-UtcDateTimeOffset -Value $loop.native_host.armed_until" in source
    assert "ConvertTo-UtcDateTimeOffset -Value $native.armed_until" in source
    assert "-Value $arm.armed_until" in source
    assert "[DateTimeOffset]::Parse([string]" not in source
