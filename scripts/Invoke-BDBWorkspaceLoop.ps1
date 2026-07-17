param(
    [ValidateSet("Prepare", "Start", "Status", "Stop")]
    [string]$Action = "Status",

    [Parameter(Mandatory = $true)]
    [string]$Root,

    [string]$Repo,

    [string]$Alias,

    [string[]]$AllowedPath = @(),

    [string]$Python,

    [ValidateRange(1, 60)]
    [int]$ArmMinutes = 30,

    [ValidateRange(1, 3600)]
    [int]$TestTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rootPath = [System.IO.Path]::GetFullPath($Root)
$statePath = Join-Path $rootPath "workspace-loop-state.json"

function Resolve-PythonExecutable([string]$Requested) {
    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        if (Test-Path -LiteralPath $Requested -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Requested).Path
        }
        return (Get-Command $Requested -ErrorAction Stop).Source
    }
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $venvPython).Path
    }
    return (Get-Command "python" -ErrorAction Stop).Source
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    $items = @(& $Executable @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Executable $($Arguments -join ' ')"
    }
    return (($items | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
}

function Invoke-Json([string]$Executable, [string[]]$Arguments) {
    $text = Invoke-Checked $Executable $Arguments
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "Expected JSON output from: $Executable $($Arguments -join ' ')"
    }
    return $text | ConvertFrom-Json
}

function Read-State() {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw "Workspace loop state is missing: $statePath"
    }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.schema -ne "bdb-workspace-loop-state-v1") {
        throw "Unsupported workspace loop state schema"
    }
    return $state
}

function Test-ProcessAlive([int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $false
    }
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Get-PromoterStatus($State) {
    $pidFile = [string]$State.promoter_pid_file
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        return [ordered]@{ running = $false; pid = $null }
    }
    $raw = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed)) {
        return [ordered]@{ running = $false; pid = $null; diagnostic = "invalid_pid_file" }
    }
    return [ordered]@{ running = (Test-ProcessAlive $parsed); pid = $parsed }
}

function Wait-ForBridgeState(
    [string]$PythonExecutable,
    [string]$BridgeConfig,
    [string]$ExpectedState,
    [int]$Attempts = 120
) {
    $last = $null
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        try {
            $last = Invoke-Json $PythonExecutable @(
                "-m", "bdb_bridge", "bridge", "status",
                "--config", $BridgeConfig,
                "--json"
            )
            if ($last.status -eq $ExpectedState) {
                return $last
            }
        }
        catch {
            $last = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Bridge did not reach $ExpectedState; last=$last"
}

function Start-Promoter($State) {
    $current = Get-PromoterStatus $State
    if ($current.running) {
        return $current
    }
    if (Test-Path -LiteralPath ([string]$State.promoter_pid_file)) {
        throw "Stale workspace promoter PID file requires inspection: $($State.promoter_pid_file)"
    }

    $stopFile = [string]$State.promoter_stop_file
    if (Test-Path -LiteralPath $stopFile) {
        Remove-Item -LiteralPath $stopFile -Force
    }

    $arguments = @(
        ('"' + [string]$State.promoter_script + '"'),
        "--config", ('"' + [string]$State.bridge_config + '"'),
        "--pid-file", ('"' + [string]$State.promoter_pid_file + '"'),
        "--stop-file", ('"' + [string]$State.promoter_stop_file + '"')
    )
    Start-Process \
        -FilePath ([string]$State.python_executable) \
        -ArgumentList $arguments \
        -RedirectStandardOutput ([string]$State.promoter_stdout) \
        -RedirectStandardError ([string]$State.promoter_stderr) \
        -WindowStyle Hidden | Out-Null

    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        Start-Sleep -Milliseconds 100
        $current = Get-PromoterStatus $State
        if ($current.running) {
            return $current
        }
    }
    throw "Workspace promoter did not publish a live PID"
}

if ($Action -eq "Prepare") {
    if ($env:OS -ne "Windows_NT") {
        throw "The workspace loop operator currently supports Windows only"
    }
    if ([string]::IsNullOrWhiteSpace($Repo)) {
        throw "Prepare requires -Repo"
    }
    if ([string]::IsNullOrWhiteSpace($Alias) -or $Alias -cnotmatch '^[a-z][a-z0-9-]{0,31}$') {
        throw "Prepare requires a safe lowercase -Alias"
    }
    if ($AllowedPath.Count -eq 0) {
        throw "Prepare requires at least one -AllowedPath"
    }
    $pythonExecutable = Resolve-PythonExecutable $Python
    $preparer = Join-Path $PSScriptRoot "prepare_workspace_loop.py"
    $arguments = @(
        $preparer,
        "--root", $rootPath,
        "--repo", ([System.IO.Path]::GetFullPath($Repo)),
        "--alias", $Alias,
        "--python", $pythonExecutable,
        "--test-timeout", [string]$TestTimeoutSeconds
    )
    foreach ($path in $AllowedPath) {
        $arguments += @("--allowed-path", $path)
    }
    $result = Invoke-Json $pythonExecutable $arguments
    [ordered]@{
        status = "PREPARED"
        alias = $result.alias
        source_repo = $result.source_repo
        source_head = $result.source_head
        bridge_config = $result.bridge_config
        allowed_paths = $result.allowed_paths
        next_action = "Start"
    } | ConvertTo-Json -Depth 8
    exit 0
}

$state = Read-State
$pythonExecutable = (Resolve-Path -LiteralPath ([string]$state.python_executable)).Path
$bridgeConfig = (Resolve-Path -LiteralPath ([string]$state.bridge_config)).Path
$nativeConfig = (Resolve-Path -LiteralPath ([string]$state.native_config)).Path

if ($Action -eq "Start") {
    $promoter = Start-Promoter $state
    $bridge = Invoke-Json $pythonExecutable @(
        "-m", "bdb_bridge", "bridge", "status",
        "--config", $bridgeConfig,
        "--json"
    )
    if ($bridge.status -eq "OFFLINE") {
        Invoke-Checked $pythonExecutable @(
            "-m", "bdb_bridge", "bridge", "start",
            "--config", $bridgeConfig,
            "--background"
        ) | Out-Null
        $bridge = Wait-ForBridgeState $pythonExecutable $bridgeConfig "RUNNING"
    }
    elseif ($bridge.status -ne "RUNNING") {
        throw "Bridge cannot start from state $($bridge.status)"
    }
    $arm = Invoke-Json $pythonExecutable @(
        "-m", "bdb_bridge", "bridge", "native-host", "arm",
        "--config", $nativeConfig,
        "--minutes", [string]$ArmMinutes
    )
    [ordered]@{
        status = "RUNNING"
        alias = $state.alias
        bridge = $bridge
        native_host = $arm
        promoter = $promoter
    } | ConvertTo-Json -Depth 10
    exit 0
}

if ($Action -eq "Status") {
    $bridge = Invoke-Json $pythonExecutable @(
        "-m", "bdb_bridge", "bridge", "status",
        "--config", $bridgeConfig,
        "--json"
    )
    $native = Invoke-Json $pythonExecutable @(
        "-m", "bdb_bridge", "bridge", "native-host", "status",
        "--config", $nativeConfig,
        "--json"
    )
    $promoter = Get-PromoterStatus $state
    $sourceStatus = Invoke-Checked "git" @(
        "-C", [string]$state.source_repo,
        "status", "--porcelain=v1"
    )
    $sourceHead = Invoke-Checked "git" @(
        "-C", [string]$state.source_repo,
        "rev-parse", "HEAD"
    )
    $ready = (
        $bridge.status -eq "RUNNING" -and
        $native.armed -eq $true -and
        $promoter.running -eq $true -and
        [string]::IsNullOrWhiteSpace($sourceStatus)
    )
    [ordered]@{
        status = if ($ready) { "READY" } else { "NOT_READY" }
        alias = $state.alias
        bridge = $bridge
        native_host = $native
        promoter = $promoter
        source_head = $sourceHead
        source_clean = [string]::IsNullOrWhiteSpace($sourceStatus)
    } | ConvertTo-Json -Depth 10
    exit 0
}

Invoke-Json $pythonExecutable @(
    "-m", "bdb_bridge", "bridge", "native-host", "disarm",
    "--config", $nativeConfig
) | Out-Null
Invoke-Checked $pythonExecutable @(
    "-m", "bdb_bridge", "bridge", "stop",
    "--config", $bridgeConfig
) | Out-Null
$bridge = Wait-ForBridgeState $pythonExecutable $bridgeConfig "OFFLINE"

$promoterBefore = Get-PromoterStatus $state
if ($promoterBefore.running) {
    [System.IO.File]::WriteAllText(
        [string]$state.promoter_stop_file,
        "stop`n",
        (New-Object System.Text.UTF8Encoding($false))
    )
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        Start-Sleep -Milliseconds 100
        $current = Get-PromoterStatus $state
        if (-not $current.running -and -not (Test-Path -LiteralPath ([string]$state.promoter_pid_file))) {
            break
        }
    }
}
$promoter = Get-PromoterStatus $state
if ($promoter.running) {
    throw "Workspace promoter did not stop cooperatively"
}
[ordered]@{
    status = "OFFLINE"
    alias = $state.alias
    bridge = $bridge
    promoter = $promoter
    artifacts_preserved = $true
} | ConvertTo-Json -Depth 10
