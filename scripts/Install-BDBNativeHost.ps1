param(
    [Parameter(Mandatory = $true)]
    [string]$HostExecutable,

    [Parameter(Mandatory = $true)]
    [string]$BridgeConfig,

    [ValidatePattern('^[a-z][a-z0-9-]{0,31}$')]
    [string]$RepositoryAlias = "default",

    [string]$ChromeExtensionId,

    [string]$EdgeExtensionId,

    [ValidateRange(0, 120)]
    [int]$MaxWaitSeconds = 30,

    [switch]$RequireWindowless
)

$ErrorActionPreference = "Stop"
$hostName = "com.bartosz.dev_bridge"
$extensionIdPattern = '^[a-p]{32}$'
$windowsGuiSubsystem = 2

function Get-PeSubsystem([string]$ExecutablePath) {
    $bytes = [System.IO.File]::ReadAllBytes($ExecutablePath)
    if ($bytes.Length -lt 64 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) {
        throw "HostExecutable does not have a valid MZ header"
    }
    $peOffset = [System.BitConverter]::ToInt32($bytes, 0x3c)
    $optionalHeader = $peOffset + 24
    if (
        $peOffset -lt 0 -or
        $optionalHeader + 70 -gt $bytes.Length -or
        $bytes[$peOffset] -ne 0x50 -or
        $bytes[$peOffset + 1] -ne 0x45 -or
        $bytes[$peOffset + 2] -ne 0 -or
        $bytes[$peOffset + 3] -ne 0
    ) {
        throw "HostExecutable does not have a complete PE header"
    }
    $magic = [System.BitConverter]::ToUInt16($bytes, $optionalHeader)
    if ($magic -ne 0x10b -and $magic -ne 0x20b) {
        throw "HostExecutable has an unsupported PE optional header"
    }
    return [System.BitConverter]::ToUInt16($bytes, $optionalHeader + 68)
}

$hostExecutablePath = (Resolve-Path -LiteralPath $HostExecutable).Path
$bridgeConfigPath = (Resolve-Path -LiteralPath $BridgeConfig).Path

if (-not (Test-Path -LiteralPath $hostExecutablePath -PathType Leaf)) {
    throw "HostExecutable must identify a file"
}
if (-not (Test-Path -LiteralPath $bridgeConfigPath -PathType Leaf)) {
    throw "BridgeConfig must identify a file"
}

$peSubsystem = $null
if ($RequireWindowless) {
    if ($env:OS -ne "Windows_NT") {
        throw "RequireWindowless is supported only on Windows"
    }
    $peSubsystem = Get-PeSubsystem $hostExecutablePath
    if ($peSubsystem -ne $windowsGuiSubsystem) {
        throw "HostExecutable PE subsystem is $peSubsystem; expected Windows GUI ($windowsGuiSubsystem)"
    }
}

$ids = @()
foreach ($candidate in @($ChromeExtensionId, $EdgeExtensionId)) {
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        continue
    }
    if ($candidate -cnotmatch $extensionIdPattern) {
        throw "Extension IDs must contain exactly 32 lowercase letters from a to p"
    }
    if ($ids -notcontains $candidate) {
        $ids += $candidate
    }
}
if ($ids.Count -eq 0) {
    throw "Provide at least one ChromeExtensionId or EdgeExtensionId"
}

$allowedOrigins = @($ids | ForEach-Object { "chrome-extension://$_/" })
$installRoot = Join-Path $env:LOCALAPPDATA "BartoszDevBridge"
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null

$nativeConfigPath = Join-Path $installRoot "native-host.json"
$hostManifestPath = Join-Path $installRoot "$hostName.json"
$repositories = [ordered]@{}
$repositories[$RepositoryAlias] = [ordered]@{
    bridge_config_path = $bridgeConfigPath
}

$nativeConfig = [ordered]@{
    schema = "bdb-native-host-config-v1"
    repositories = $repositories
    allowed_origins = $allowedOrigins
    state_path = (Join-Path $installRoot "native-host-arm.json")
    session_store_path = (Join-Path $installRoot "native-host-sessions.json")
    max_wait_seconds = $MaxWaitSeconds
    max_message_bytes = 1048576
}

$hostManifest = [ordered]@{
    name = $hostName
    description = "Bartosz Dev Bridge Direct Lane"
    path = $hostExecutablePath
    type = "stdio"
    allowed_origins = $allowedOrigins
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $nativeConfigPath,
    ($nativeConfig | ConvertTo-Json -Depth 8),
    $utf8NoBom
)
[System.IO.File]::WriteAllText(
    $hostManifestPath,
    ($hostManifest | ConvertTo-Json -Depth 6),
    $utf8NoBom
)

$registryKeys = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName"
)
foreach ($key in $registryKeys) {
    New-Item -Path $key -Force | Out-Null
    Set-Item -Path $key -Value $hostManifestPath
}

$result = [ordered]@{
    installed = $true
    host_name = $hostName
    repository_alias = $RepositoryAlias
    browsers = @("chrome", "edge")
    allowed_origin_count = $allowedOrigins.Count
    config = $nativeConfigPath
    manifest = $hostManifestPath
    executable = $hostExecutablePath
    windowless_required = [bool]$RequireWindowless
    pe_subsystem = $peSubsystem
}
$result | ConvertTo-Json -Depth 5
