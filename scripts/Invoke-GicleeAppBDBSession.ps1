[CmdletBinding()]
param(
    [ValidateSet("Start", "Status", "Stop")]
    [string]$Action = "Start"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Alias = "gicleeapp"
$Root = Join-Path $env:LOCALAPPDATA "BartoszDevBridge\workspaces\$Alias"
$LoopScript = Join-Path $PSScriptRoot "Invoke-BDBWorkspaceLoop.ps1"
$KeeperScript = Join-Path $PSScriptRoot "Invoke-BDBSessionArmKeeper.ps1"

foreach ($required in @($Root, $LoopScript, $KeeperScript)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Brak wymaganego elementu: $required"
    }
}

switch ($Action) {
    "Start" {
        $loopRaw = & $LoopScript `
            -Action Start `
            -Root $Root `
            -ArmMinutes 60 | Out-String

        if ($LASTEXITCODE -ne 0) {
            throw "Nie udało się uruchomić Local Bridge."
        }

        $loop = $loopRaw | ConvertFrom-Json

        $keeperRaw = & $KeeperScript `
            -Action Start `
            -Root $Root `
            -SessionHours 12 `
            -IdleMinutes 90 `
            -LeaseMinutes 60 `
            -RenewBeforeMinutes 10 `
            -PollSeconds 60 | Out-String

        if ($LASTEXITCODE -ne 0) {
            throw "Nie udało się uruchomić Session Arm Keepera."
        }

        $keeper = $keeperRaw | ConvertFrom-Json
        $statusRaw = & $LoopScript -Action Status -Root $Root | Out-String
        $status = $statusRaw | ConvertFrom-Json

        [ordered]@{
            schema = "bdb-gicleeapp-session-v1"
            status = "READY"
            alias = $Alias
            transport = "local_assisted"
            auto_mode = $false
            bridge = $status.bridge
            native_host = $status.native_host
            controlled_clean = $status.controlled_clean
            source_changes_outside_scope = $status.source_changes_outside_scope
            session_arm = $keeper
            safety = [ordered]@{
                github_transport = $false
                github_push = $false
                shopify_operation = $false
                shopify_publish = $false
                max_session_hours = 12
                idle_timeout_minutes = 90
            }
        } | ConvertTo-Json -Depth 30
    }

    "Status" {
        $loopRaw = & $LoopScript -Action Status -Root $Root | Out-String
        if ($LASTEXITCODE -ne 0) {
            throw "Nie udało się odczytać statusu Local Bridge."
        }

        $keeperRaw = & $KeeperScript -Action Status -Root $Root | Out-String
        if ($LASTEXITCODE -ne 0) {
            throw "Nie udało się odczytać statusu Session Arm Keepera."
        }

        [ordered]@{
            schema = "bdb-gicleeapp-session-status-v1"
            alias = $Alias
            bridge = ($loopRaw | ConvertFrom-Json)
            session_arm = ($keeperRaw | ConvertFrom-Json)
        } | ConvertTo-Json -Depth 30
    }

    "Stop" {
        $keeperRaw = & $KeeperScript -Action Stop -Root $Root | Out-String
        if ($LASTEXITCODE -ne 0) {
            throw "Nie udało się zatrzymać Session Arm Keepera."
        }

        $loopRaw = & $LoopScript -Action Stop -Root $Root | Out-String
        if ($LASTEXITCODE -ne 0) {
            throw "Nie udało się zatrzymać Local Bridge."
        }

        [ordered]@{
            schema = "bdb-gicleeapp-session-stop-v1"
            alias = $Alias
            status = "OFFLINE"
            session_arm = ($keeperRaw | ConvertFrom-Json)
            bridge = ($loopRaw | ConvertFrom-Json)
            artifacts_preserved = $true
        } | ConvertTo-Json -Depth 30
    }
}
