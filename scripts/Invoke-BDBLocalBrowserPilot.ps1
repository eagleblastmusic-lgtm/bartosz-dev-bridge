param(
    [ValidateSet("Setup", "Status", "Stop")]
    [string]$Action = "Setup",

    [ValidateSet("Chrome", "Edge")]
    [string]$Browser = "Chrome",

    [string]$ExtensionId,

    [string]$Python = "python",

    [string]$Root = (Join-Path $env:LOCALAPPDATA "BartoszDevBridge\local-browser-pilot"),

    [ValidateRange(1, 60)]
    [int]$ArmMinutes = 10
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rootPath = [System.IO.Path]::GetFullPath($Root)
$statePath = Join-Path $rootPath "operator-state.json"
$installRoot = Join-Path $env:LOCALAPPDATA "BartoszDevBridge"
$nativeConfigPath = Join-Path $installRoot "native-host.json"
$nativeManifestPath = Join-Path $installRoot "com.bartosz.dev_bridge.json"
$extensionIdPattern = '^[a-p]{32}$'

function Resolve-Executable([string]$Name) {
    return (Get-Command $Name -ErrorAction Stop).Source
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    $output = & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE: $Executable $($Arguments -join ' ')"
    }
    return @($output)
}

function Invoke-Json([string]$Executable, [string[]]$Arguments) {
    $lines = Invoke-Checked $Executable $Arguments
    $text = ($lines -join [Environment]::NewLine).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "Expected JSON output from: $Executable $($Arguments -join ' ')"
    }
    return $text | ConvertFrom-Json
}

function Read-State() {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw "Pilot state is missing: $statePath"
    }
    return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
}

function Wait-ForRunning([string]$PythonExecutable, [string]$BridgeConfig) {
    $last = $null
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        try {
            $last = Invoke-Json $PythonExecutable @(
                "-m", "bdb_bridge", "bridge", "status",
                "--config", $BridgeConfig,
                "--json"
            )
            if ($last.status -eq "RUNNING") {
                return $last
            }
        }
        catch {
            $last = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Bridge did not reach RUNNING; last=$last"
}

if ($Action -eq "Setup") {
    if ($env:OS -ne "Windows_NT") {
        throw "The local browser pilot bootstrap supports Windows only"
    }
    if ([string]::IsNullOrWhiteSpace($ExtensionId) -or $ExtensionId -cnotmatch $extensionIdPattern) {
        throw "ExtensionId must contain exactly 32 lowercase letters from a to p"
    }
    if (Test-Path -LiteralPath $rootPath) {
        throw "Pilot root already exists. Use -Action Status or -Action Stop: $rootPath"
    }
    foreach ($existing in @($nativeConfigPath, $nativeManifestPath)) {
        if (Test-Path -LiteralPath $existing) {
            throw "Existing Native Host installation detected and will not be overwritten: $existing"
        }
    }
    foreach ($key in @(
        "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.bartosz.dev_bridge",
        "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.bartosz.dev_bridge"
    )) {
        if (Test-Path -LiteralPath $key) {
            throw "Existing Native Host registry entry detected and will not be overwritten: $key"
        }
    }

    $gitStatus = (& git -C $repoRoot status --porcelain=v1)
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot inspect the Bridge checkout with Git"
    }
    if (-not [string]::IsNullOrWhiteSpace(($gitStatus -join ""))) {
        throw "Bridge checkout must be clean before pilot setup"
    }
    $implementationSha = (& git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot resolve the Bridge implementation HEAD"
    }

    $bootstrapPython = Resolve-Executable $Python
    $venvRoot = Join-Path $repoRoot ".venv"
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-Checked $bootstrapPython @("-m", "venv", $venvRoot) | Out-Null
    }
    $venvPython = (Resolve-Path -LiteralPath $venvPython).Path
    Invoke-Checked $venvPython @(
        "-m", "pip", "install", "--disable-pip-version-check", "-e", $repoRoot
    ) | Out-Null

    $preparer = Join-Path $PSScriptRoot "prepare_local_browser_pilot.py"
    Invoke-Checked $venvPython @(
        $preparer,
        "--root", $rootPath,
        "--python", $venvPython
    ) | Out-Null

    $setupPath = Join-Path $rootPath "browser-pilot-setup.json"
    $setup = Get-Content -LiteralPath $setupPath -Raw | ConvertFrom-Json
    if ($setup.status -ne "prepared" -or $setup.repo_alias -ne "pilot") {
        throw "Synthetic browser pilot preparation did not finish safely"
    }

    $hostExecutable = Join-Path $venvRoot "Scripts\bdb-native-host.exe"
    $installer = Join-Path $PSScriptRoot "Install-BDBNativeHost.ps1"
    $installArguments = @{
        HostExecutable = $hostExecutable
        BridgeConfig = [string]$setup.bridge_config
        RepositoryAlias = "pilot"
        MaxWaitSeconds = 30
    }
    if ($Browser -eq "Chrome") {
        $installArguments.ChromeExtensionId = $ExtensionId
    }
    else {
        $installArguments.EdgeExtensionId = $ExtensionId
    }
    $installResult = & $installer @installArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native Host installer failed with exit code $LASTEXITCODE"
    }
    $install = ($installResult -join [Environment]::NewLine) | ConvertFrom-Json

    Invoke-Checked $venvPython @(
        "-m", "bdb_bridge", "bridge", "start",
        "--config", [string]$setup.bridge_config,
        "--background"
    ) | Out-Null
    $bridge = Wait-ForRunning $venvPython ([string]$setup.bridge_config)
    $arm = Invoke-Json $venvPython @(
        "-m", "bdb_bridge", "bridge", "native-host", "arm",
        "--config", $nativeConfigPath,
        "--minutes", [string]$ArmMinutes
    )

    $state = [ordered]@{
        schema = "bdb-local-browser-pilot-operator-state-v1"
        status = "running"
        implementation_sha = $implementationSha
        root = $rootPath
        browser = $Browser
        extension_id = $ExtensionId
        repo_alias = "pilot"
        bridge_config = [string]$setup.bridge_config
        native_config = $nativeConfigPath
        native_manifest = $nativeManifestPath
        python_executable = $venvPython
        extension_directory = [string]$setup.extension_directory
        read_action = [string]$setup.read_action
        patch_action = [string]$setup.patch_action
        prepared_at = [DateTime]::UtcNow.ToString("o")
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $statePath,
        ($state | ConvertTo-Json -Depth 8),
        $utf8NoBom
    )

    [ordered]@{
        status = "RUNNING"
        implementation_sha = $implementationSha
        repo_alias = "pilot"
        browser = $Browser
        extension_id = $ExtensionId
        extension_directory = [string]$setup.extension_directory
        read_action = [string]$setup.read_action
        patch_action = [string]$setup.patch_action
        bridge = $bridge
        native_host = $install
        arm = $arm
        safety = @(
            "synthetic repository only",
            "three-path local allowlist",
            "no existing Native Host registration overwritten",
            "no administrator rights or network port",
            "artifacts preserved on stop"
        )
    } | ConvertTo-Json -Depth 10
    exit 0
}

$state = Read-State
$pythonExecutable = (Resolve-Path -LiteralPath ([string]$state.python_executable)).Path
$bridgeConfig = (Resolve-Path -LiteralPath ([string]$state.bridge_config)).Path
$resolvedNativeConfig = (Resolve-Path -LiteralPath ([string]$state.native_config)).Path

if ($Action -eq "Status") {
    $bridge = Invoke-Json $pythonExecutable @(
        "-m", "bdb_bridge", "bridge", "status",
        "--config", $bridgeConfig,
        "--json"
    )
    $native = Invoke-Json $pythonExecutable @(
        "-m", "bdb_bridge", "bridge", "native-host", "status",
        "--config", $resolvedNativeConfig,
        "--json"
    )
    [ordered]@{
        root = $rootPath
        repo_alias = [string]$state.repo_alias
        bridge = $bridge
        native_host = $native
        read_action = [string]$state.read_action
        patch_action = [string]$state.patch_action
    } | ConvertTo-Json -Depth 10
    exit 0
}

try {
    Invoke-Checked $pythonExecutable @(
        "-m", "bdb_bridge", "bridge", "native-host", "disarm",
        "--config", $resolvedNativeConfig
    ) | Out-Null
}
catch {
    Write-Warning "Native Host disarm did not complete cleanly: $($_.Exception.Message)"
}
Invoke-Checked $pythonExecutable @(
    "-m", "bdb_bridge", "bridge", "stop",
    "--config", $bridgeConfig
) | Out-Null

$state.status = "stopped"
$state.stopped_at = [DateTime]::UtcNow.ToString("o")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $statePath,
    ($state | ConvertTo-Json -Depth 8),
    $utf8NoBom
)

[ordered]@{
    status = "STOP_REQUESTED"
    root = $rootPath
    artifacts_preserved = $true
    native_registration_preserved = $true
    note = "No repository, worktree, Journal, configuration or registry key was deleted."
} | ConvertTo-Json -Depth 5
