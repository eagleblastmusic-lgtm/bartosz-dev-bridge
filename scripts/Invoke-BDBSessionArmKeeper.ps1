[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Start", "Run", "Status", "Stop")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$Root,

    [ValidateRange(1, 12)]
    [int]$SessionHours = 12,

    [ValidateRange(15, 180)]
    [int]$IdleMinutes = 90,

    [ValidateRange(5, 60)]
    [int]$LeaseMinutes = 60,

    [ValidateRange(1, 30)]
    [int]$RenewBeforeMinutes = 10,

    [ValidateRange(15, 300)]
    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$BridgeRepo = Split-Path -Parent $PSScriptRoot
$LoopScript = Join-Path $PSScriptRoot "Invoke-BDBWorkspaceLoop.ps1"
$RuntimeDir = Join-Path $Root "runtime\session-arm"
$KeeperStatePath = Join-Path $RuntimeDir "keeper-state.json"
$KeeperPidPath = Join-Path $RuntimeDir "keeper.pid"
$KeeperStopPath = Join-Path $RuntimeDir "keeper.stop"
$KeeperLockPath = Join-Path $RuntimeDir "keeper.lock"
$KeeperLogPath = Join-Path $RuntimeDir "keeper.log"
$WorkspaceStatePath = Join-Path $Root "workspace-loop-state.json"

function Write-JsonNoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $json = $Value | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText(
        $Path,
        $json + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Read-Json {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }

    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Write-KeeperLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    $line = "{0} {1}" -f [DateTime]::UtcNow.ToString("o"), $Message
    Add-Content -LiteralPath $KeeperLogPath -Value $line -Encoding utf8
}

function Get-WorkspaceState {
    $state = Read-Json -Path $WorkspaceStatePath
    if ($null -eq $state) {
        throw "Brak workspace-loop-state.json: $WorkspaceStatePath"
    }
    if ([string]$state.alias -ne "gicleeapp") {
        throw "Session Arm Keeper jest dozwolony wyłącznie dla aliasu gicleeapp."
    }

    foreach ($name in @("python_executable", "native_config", "bridge_config")) {
        if (-not $state.PSObject.Properties[$name]) {
            throw "Brak pola $name w workspace-loop-state.json."
        }
    }

    return $state
}

function Get-LoopStatus {
    $raw = & $LoopScript -Action Status -Root $Root | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Invoke-BDBWorkspaceLoop Status zakończył się kodem $LASTEXITCODE."
    }
    return $raw | ConvertFrom-Json
}

function Get-NativeStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$NativeConfig
    )

    $raw = & $Python -m bdb_bridge bridge native-host status `
        --config $NativeConfig `
        --json 2>&1 | Out-String

    if ($LASTEXITCODE -ne 0) {
        throw "Odczyt statusu Native Hosta nie powiódł się: $raw"
    }

    return $raw | ConvertFrom-Json
}

function Invoke-Arm {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$NativeConfig
    )

    $raw = & $Python -m bdb_bridge bridge native-host arm `
        --config $NativeConfig `
        --minutes $LeaseMinutes 2>&1 | Out-String

    if ($LASTEXITCODE -ne 0) {
        throw "Odnowienie uzbrojenia nie powiodło się: $raw"
    }

    return $raw | ConvertFrom-Json
}

function Invoke-DisarmIfOwned {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$NativeConfig,
        [AllowNull()][string]$OwnedGenerationId
    )

    if ([string]::IsNullOrWhiteSpace($OwnedGenerationId)) {
        return $false
    }

    $native = Get-NativeStatus -Python $Python -NativeConfig $NativeConfig
    if (
        $native.armed -eq $true -and
        [string]$native.generation_id -eq $OwnedGenerationId
    ) {
        $raw = & $Python -m bdb_bridge bridge native-host disarm `
            --config $NativeConfig 2>&1 | Out-String

        if ($LASTEXITCODE -ne 0) {
            throw "Rozbrojenie Native Hosta nie powiodło się: $raw"
        }

        Write-KeeperLog -Message "DISARM generation_id=$OwnedGenerationId"
        return $true
    }

    return $false
}

function Test-ProcessAlive {
    param([AllowNull()][object]$ProcessId)

    if ($null -eq $ProcessId) {
        return $false
    }

    $number = 0
    if (-not [int]::TryParse([string]$ProcessId, [ref]$number)) {
        return $false
    }

    return $null -ne (Get-Process -Id $number -ErrorAction SilentlyContinue)
}

function Get-LatestRuntimeActivityUtc {
    $roots = @(
        (Join-Path $Root "runtime\direct_spool\inbox"),
        (Join-Path $Root "runtime\direct_spool\results"),
        (Join-Path $Root "runtime\promotions")
    )

    $latest = $null
    foreach ($candidate in $roots) {
        if (-not (Test-Path -LiteralPath $candidate)) {
            continue
        }

        Get-ChildItem `
            -LiteralPath $candidate `
            -File `
            -Recurse `
            -ErrorAction SilentlyContinue |
            ForEach-Object {
                if ($null -eq $latest -or $_.LastWriteTimeUtc -gt $latest) {
                    $latest = $_.LastWriteTimeUtc
                }
            }
    }

    return $latest
}

function Get-InFlightCommandCount {
    param([Parameter(Mandatory = $true)][datetime]$CutoffUtc)

    $resultIds = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )

    $resultsRoot = Join-Path $Root "runtime\direct_spool\results"
    if (Test-Path -LiteralPath $resultsRoot) {
        Get-ChildItem `
            -LiteralPath $resultsRoot `
            -File `
            -Filter "*.json" `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTimeUtc -ge $CutoffUtc } |
            ForEach-Object {
                try {
                    $document = Get-Content -LiteralPath $_.FullName -Raw |
                        ConvertFrom-Json
                    if ($document.command_id) {
                        [void]$resultIds.Add([string]$document.command_id)
                    }
                }
                catch {
                }
            }
    }

    $pending = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )

    $inboxRoot = Join-Path $Root "runtime\direct_spool\inbox"
    if (Test-Path -LiteralPath $inboxRoot) {
        Get-ChildItem `
            -LiteralPath $inboxRoot `
            -File `
            -Filter "*.json" `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTimeUtc -ge $CutoffUtc } |
            ForEach-Object {
                try {
                    $document = Get-Content -LiteralPath $_.FullName -Raw |
                        ConvertFrom-Json
                    $commandId = [string]$document.command.command_id
                    if (
                        -not [string]::IsNullOrWhiteSpace($commandId) -and
                        -not $resultIds.Contains($commandId)
                    ) {
                        [void]$pending.Add($commandId)
                    }
                }
                catch {
                }
            }
    }

    return $pending.Count
}

function Write-KeeperState {
    param(
        [Parameter(Mandatory = $true)][bool]$Running,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][datetime]$SessionStartedUtc,
        [Parameter(Mandatory = $true)][datetime]$SessionDeadlineUtc,
        [Parameter(Mandatory = $true)][datetime]$LastActivityUtc,
        [Parameter(Mandatory = $true)][int]$RenewCount,
        [AllowNull()][string]$OwnedGenerationId,
        [AllowNull()][object]$ArmedUntil,
        [Parameter(Mandatory = $true)][int]$InFlightCount,
        [AllowNull()][string]$StopReason,
        [AllowNull()][string]$BridgeInstanceId
    )

    $armedUntilText = $null
    if ($null -ne $ArmedUntil) {
        $armedUntilText = ([DateTimeOffset]$ArmedUntil).UtcDateTime.ToString("o")
    }

    $document = [ordered]@{
        schema = "bdb-session-arm-keeper-v1"
        alias = "gicleeapp"
        running = $Running
        status = $Status
        pid = $PID
        bridge_instance_id = $BridgeInstanceId
        session_started_at = $SessionStartedUtc.ToString("o")
        session_deadline = $SessionDeadlineUtc.ToString("o")
        max_session_hours = $SessionHours
        idle_timeout_minutes = $IdleMinutes
        lease_minutes = $LeaseMinutes
        renew_before_minutes = $RenewBeforeMinutes
        poll_seconds = $PollSeconds
        last_activity_at = $LastActivityUtc.ToString("o")
        idle_deadline = $LastActivityUtc.AddMinutes($IdleMinutes).ToString("o")
        renew_count = $RenewCount
        generation_id = $OwnedGenerationId
        armed_until = $armedUntilText
        in_flight_count = $InFlightCount
        stop_reason = $StopReason
        updated_at = [DateTime]::UtcNow.ToString("o")
        log = $KeeperLogPath
    }

    Write-JsonNoBom -Path $KeeperStatePath -Value $document
}

function Get-KeeperStatus {
    $state = Read-Json -Path $KeeperStatePath
    $pidValue = $null
    if (Test-Path -LiteralPath $KeeperPidPath -PathType Leaf) {
        $pidValue = (Get-Content -LiteralPath $KeeperPidPath -Raw).Trim()
    }

    $running = Test-ProcessAlive -ProcessId $pidValue

    if ($null -eq $state) {
        return [ordered]@{
            schema = "bdb-session-arm-keeper-status-v1"
            alias = "gicleeapp"
            running = $running
            pid = $pidValue
            status = $(if ($running) { "RUNNING" } else { "OFFLINE" })
            state = $null
            log = $KeeperLogPath
        }
    }

    return [ordered]@{
        schema = "bdb-session-arm-keeper-status-v1"
        alias = "gicleeapp"
        running = $running
        pid = $pidValue
        status = $(if ($running) { "RUNNING" } else { [string]$state.status })
        state = $state
        log = $KeeperLogPath
    }
}

if (-not (Test-Path -LiteralPath $LoopScript -PathType Leaf)) {
    throw "Brak operatora workspace loop: $LoopScript"
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

switch ($Action) {
    "Status" {
        Get-KeeperStatus | ConvertTo-Json -Depth 20
        exit 0
    }

    "Start" {
        $existing = Get-KeeperStatus
        if ($existing.running -eq $true) {
            $existing | ConvertTo-Json -Depth 20
            exit 0
        }

        $workspace = Get-WorkspaceState
        $loop = Get-LoopStatus

        if (
            [string]$loop.status -ne "READY" -or
            [string]$loop.bridge.status -ne "RUNNING" -or
            $loop.bridge.pid_alive -ne $true -or
            $loop.bridge.lock_held -ne $true -or
            $loop.controlled_clean -ne $true
        ) {
            $loop | ConvertTo-Json -Depth 20 | Out-Host
            throw "Workspace gicleeapp nie jest w bezpiecznym stanie READY/controlled_clean."
        }

        Remove-Item -LiteralPath $KeeperStopPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $KeeperPidPath -Force -ErrorAction SilentlyContinue

        $pwsh = Join-Path $PSHOME "pwsh.exe"
        if (-not (Test-Path -LiteralPath $pwsh -PathType Leaf)) {
            $pwsh = (Get-Command pwsh -ErrorAction Stop).Source
        }

        $arguments = @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", ('"{0}"' -f $PSCommandPath),
            "-Action", "Run",
            "-Root", ('"{0}"' -f $Root),
            "-SessionHours", [string]$SessionHours,
            "-IdleMinutes", [string]$IdleMinutes,
            "-LeaseMinutes", [string]$LeaseMinutes,
            "-RenewBeforeMinutes", [string]$RenewBeforeMinutes,
            "-PollSeconds", [string]$PollSeconds
        )

        $process = Start-Process `
            -FilePath $pwsh `
            -ArgumentList $arguments `
            -WindowStyle Hidden `
            -PassThru

        $deadline = [DateTime]::UtcNow.AddSeconds(12)
        do {
            Start-Sleep -Milliseconds 250
            $status = Get-KeeperStatus
            if ($status.running -eq $true) {
                $status | ConvertTo-Json -Depth 20
                exit 0
            }
        } while ([DateTime]::UtcNow -lt $deadline)

        throw "Session Arm Keeper nie uruchomił się. PID procesu startowego: $($process.Id)"
    }

    "Stop" {
        $status = Get-KeeperStatus
        if ($status.running -ne $true) {
            $status | ConvertTo-Json -Depth 20
            exit 0
        }

        [System.IO.File]::WriteAllText(
            $KeeperStopPath,
            [DateTime]::UtcNow.ToString("o") + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )

        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 250
            $status = Get-KeeperStatus
            if ($status.running -ne $true) {
                $status | ConvertTo-Json -Depth 20
                exit 0
            }
        } while ([DateTime]::UtcNow -lt $deadline)

        throw "Session Arm Keeper nie zatrzymał się kooperacyjnie w ciągu 20 sekund."
    }

    "Run" {
        $lockStream = $null
        $ownedGenerationId = $null
        $python = $null
        $nativeConfig = $null
        $bridgeInstanceId = $null
        $sessionStartedUtc = [DateTime]::UtcNow
        $sessionDeadlineUtc = $sessionStartedUtc.AddHours($SessionHours)
        $lastActivityUtc = $sessionStartedUtc
        $lastArmedUntil = $null
        $renewCount = 0
        $inFlightCount = 0
        $stopReason = $null
        $shouldDisarm = $true
        $dirtySinceUtc = $null

        try {
            $lockStream = [System.IO.File]::Open(
                $KeeperLockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )

            [System.IO.File]::WriteAllText(
                $KeeperPidPath,
                [string]$PID,
                [System.Text.UTF8Encoding]::new($false)
            )

            $workspace = Get-WorkspaceState
            $python = (Resolve-Path -LiteralPath ([string]$workspace.python_executable)).Path
            $nativeConfig = (Resolve-Path -LiteralPath ([string]$workspace.native_config)).Path
            $nativeConfigHash = (Get-FileHash -LiteralPath $nativeConfig -Algorithm SHA256).Hash

            $loop = Get-LoopStatus
            if (
                [string]$loop.status -ne "READY" -or
                [string]$loop.bridge.status -ne "RUNNING" -or
                $loop.bridge.pid_alive -ne $true -or
                $loop.bridge.lock_held -ne $true -or
                $loop.controlled_clean -ne $true
            ) {
                throw "Workspace nie jest READY podczas uruchamiania keepera."
            }

            $bridgeInstanceId = [string]$loop.bridge.instance_id
            $ownedGenerationId = [string]$loop.native_host.generation_id
            $lastArmedUntil = [DateTimeOffset]::Parse([string]$loop.native_host.armed_until)

            $activity = Get-LatestRuntimeActivityUtc
            if ($null -ne $activity -and $activity -gt $lastActivityUtc) {
                $lastActivityUtc = $activity
            }

            Write-KeeperLog -Message (
                "START bridge_instance_id={0} generation_id={1} session_deadline={2}" -f
                $bridgeInstanceId,
                $ownedGenerationId,
                $sessionDeadlineUtc.ToString("o")
            )

            while ($true) {
                $now = [DateTime]::UtcNow

                if (Test-Path -LiteralPath $KeeperStopPath -PathType Leaf) {
                    $stopReason = "manual_stop"
                    break
                }

                if ($now -ge $sessionDeadlineUtc) {
                    $stopReason = "session_max_reached"
                    break
                }

                $loop = Get-LoopStatus

                if (
                    [string]$loop.bridge.status -ne "RUNNING" -or
                    $loop.bridge.pid_alive -ne $true -or
                    $loop.bridge.lock_held -ne $true
                ) {
                    $stopReason = "bridge_not_running"
                    break
                }

                if ([string]$loop.bridge.instance_id -ne $bridgeInstanceId) {
                    $stopReason = "bridge_instance_changed"
                    $shouldDisarm = $false
                    break
                }

                $currentNativeHash = (
                    Get-FileHash -LiteralPath $nativeConfig -Algorithm SHA256
                ).Hash
                if ($currentNativeHash -ne $nativeConfigHash) {
                    $stopReason = "native_config_changed"
                    $shouldDisarm = $false
                    break
                }

                $activity = Get-LatestRuntimeActivityUtc
                if ($null -ne $activity -and $activity -gt $lastActivityUtc) {
                    $lastActivityUtc = $activity
                }

                $inFlightCount = Get-InFlightCommandCount `
                    -CutoffUtc $sessionStartedUtc.AddMinutes(-5)

                $controlledClean = ($loop.controlled_clean -eq $true)
                if ($controlledClean) {
                    $dirtySinceUtc = $null
                }
                elseif ($null -eq $dirtySinceUtc) {
                    $dirtySinceUtc = $now
                }

                if (
                    -not $controlledClean -and
                    $inFlightCount -eq 0 -and
                    $null -ne $dirtySinceUtc -and
                    ($now - $dirtySinceUtc).TotalMinutes -ge 10
                ) {
                    $stopReason = "controlled_scope_dirty"
                    break
                }

                if (
                    $inFlightCount -eq 0 -and
                    ($now - $lastActivityUtc).TotalMinutes -ge $IdleMinutes
                ) {
                    $stopReason = "idle_timeout"
                    break
                }

                $native = Get-NativeStatus -Python $python -NativeConfig $nativeConfig
                $armedUntil = $null
                $remainingMinutes = -1.0

                if ($native.armed -eq $true -and $native.armed_until) {
                    $armedUntil = [DateTimeOffset]::Parse([string]$native.armed_until)
                    $remainingMinutes = (
                        $armedUntil.UtcDateTime - $now
                    ).TotalMinutes
                }

                $normalRenew = (
                    $controlledClean -and
                    $inFlightCount -eq 0 -and
                    (
                        $native.armed -ne $true -or
                        $remainingMinutes -le $RenewBeforeMinutes
                    )
                )

                $emergencyRenew = (
                    $inFlightCount -gt 0 -and
                    (
                        $native.armed -ne $true -or
                        $remainingMinutes -le 2
                    )
                )

                if ($normalRenew -or $emergencyRenew) {
                    $arm = Invoke-Arm -Python $python -NativeConfig $nativeConfig
                    $ownedGenerationId = [string]$arm.generation_id
                    $lastArmedUntil = [DateTimeOffset]::Parse(
                        [string]$arm.armed_until
                    )
                    $renewCount += 1

                    Write-KeeperLog -Message (
                        "RENEW generation_id={0} armed_until={1} emergency={2}" -f
                        $ownedGenerationId,
                        $lastArmedUntil.UtcDateTime.ToString("o"),
                        $emergencyRenew
                    )
                }
                elseif ($native.armed -eq $true) {
                    $ownedGenerationId = [string]$native.generation_id
                    $lastArmedUntil = $armedUntil
                }

                Write-KeeperState `
                    -Running $true `
                    -Status "RUNNING" `
                    -SessionStartedUtc $sessionStartedUtc `
                    -SessionDeadlineUtc $sessionDeadlineUtc `
                    -LastActivityUtc $lastActivityUtc `
                    -RenewCount $renewCount `
                    -OwnedGenerationId $ownedGenerationId `
                    -ArmedUntil $lastArmedUntil `
                    -InFlightCount $inFlightCount `
                    -StopReason $null `
                    -BridgeInstanceId $bridgeInstanceId

                for ($second = 0; $second -lt $PollSeconds; $second++) {
                    if (Test-Path -LiteralPath $KeeperStopPath -PathType Leaf) {
                        break
                    }
                    Start-Sleep -Seconds 1
                }
            }
        }
        catch {
            $stopReason = "error: $($_.Exception.Message)"
            Write-KeeperLog -Message $stopReason
            throw
        }
        finally {
            if (
                $shouldDisarm -and
                -not [string]::IsNullOrWhiteSpace($python) -and
                -not [string]::IsNullOrWhiteSpace($nativeConfig)
            ) {
                try {
                    [void](Invoke-DisarmIfOwned `
                        -Python $python `
                        -NativeConfig $nativeConfig `
                        -OwnedGenerationId $ownedGenerationId)
                }
                catch {
                    Write-KeeperLog -Message (
                        "DISARM_ERROR {0}" -f $_.Exception.Message
                    )
                }
            }

            if ($null -eq $stopReason) {
                $stopReason = "stopped"
            }

            try {
                Write-KeeperState `
                    -Running $false `
                    -Status "OFFLINE" `
                    -SessionStartedUtc $sessionStartedUtc `
                    -SessionDeadlineUtc $sessionDeadlineUtc `
                    -LastActivityUtc $lastActivityUtc `
                    -RenewCount $renewCount `
                    -OwnedGenerationId $ownedGenerationId `
                    -ArmedUntil $lastArmedUntil `
                    -InFlightCount $inFlightCount `
                    -StopReason $stopReason `
                    -BridgeInstanceId $bridgeInstanceId
            }
            catch {
            }

            Write-KeeperLog -Message "STOP reason=$stopReason"

            Remove-Item `
                -LiteralPath $KeeperPidPath `
                -Force `
                -ErrorAction SilentlyContinue

            Remove-Item `
                -LiteralPath $KeeperStopPath `
                -Force `
                -ErrorAction SilentlyContinue

            if ($null -ne $lockStream) {
                $lockStream.Dispose()
            }
        }

        exit 0
    }
}
