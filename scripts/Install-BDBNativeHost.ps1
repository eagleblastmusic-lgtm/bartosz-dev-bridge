param(
    [Parameter(Mandatory = $true)]
    [string]$HostExecutable,

    [Parameter(Mandatory = $true)]
    [string]$BridgeConfig,

    [string]$ChromeExtensionId,

    [string]$EdgeExtensionId,

    [ValidateRange(0, 120)]
    [int]$MaxWaitSeconds = 30
)

$ErrorActionPreference = "Stop"
$hostName = "com.bartosz.dev_bridge"
$extensionIdPattern = '^[a-p]{32}$'

$hostExecutablePath = (Resolve-Path -LiteralPath $HostExecutable).Path
$bridgeConfigPath = (Resolve-Path -LiteralPath $BridgeConfig).Path

if (-not (Test-Path -LiteralPath $hostExecutablePath -PathType Leaf)) {
    throw "HostExecutable must identify a file"
}
if (-not (Test-Path -LiteralPath $bridgeConfigPath -PathType Leaf)) {
    throw "BridgeConfig must identify a file"
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

$nativeConfig = [ordered]@{
    schema = "bdb-native-host-config-v1"
    bridge_config_path = $bridgeConfigPath
    allowed_origins = $allowedOrigins
    state_path = (Join-Path $installRoot "native-host-arm.json")
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
    ($nativeConfig | ConvertTo-Json -Depth 6),
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
    browsers = @("chrome", "edge")
    allowed_origin_count = $allowedOrigins.Count
    config = $nativeConfigPath
    manifest = $hostManifestPath
}
$result | ConvertTo-Json -Depth 5
