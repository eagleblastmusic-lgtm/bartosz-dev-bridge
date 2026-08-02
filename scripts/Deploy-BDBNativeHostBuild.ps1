[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$HostDirectory,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
    [string]$BuildLabel
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$source = (Resolve-Path -LiteralPath $HostDirectory).Path
$sourceExecutable = Join-Path $source "BDB-Native-Host.exe"
if (-not (Test-Path -LiteralPath $sourceExecutable -PathType Leaf)) {
    throw "HostDirectory does not contain BDB-Native-Host.exe"
}

$installRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "BartoszDevBridge"))
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $installRoot "NativeHostBuilds"))
$target = [System.IO.Path]::GetFullPath((Join-Path $buildRoot $BuildLabel))
if (-not $target.StartsWith($buildRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe Native Host build target"
}
if (Test-Path -LiteralPath $target) {
    throw "Native Host build target already exists: $target"
}

$manifestPath = Join-Path $installRoot "com.bartosz.dev_bridge.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Installed Native Host manifest is missing"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.name -cne "com.bartosz.dev_bridge" -or $manifest.type -cne "stdio") {
    throw "Installed Native Host manifest identity is invalid"
}

New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $target -Recurse
$targetExecutable = (Resolve-Path -LiteralPath (Join-Path $target "BDB-Native-Host.exe")).Path
$manifest.path = $targetExecutable

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$temporary = Join-Path $installRoot ".com.bartosz.dev_bridge.$timestamp.tmp"
$backup = Join-Path $installRoot "com.bartosz.dev_bridge.pre-$BuildLabel-$timestamp.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $temporary,
    ($manifest | ConvertTo-Json -Depth 8),
    $utf8NoBom
)
[System.IO.File]::Replace($temporary, $manifestPath, $backup, $true)

[ordered]@{
    status = "deployed"
    build_label = $BuildLabel
    executable = $targetExecutable
    manifest = $manifestPath
    manifest_backup = $backup
    native_config_preserved = (Join-Path $installRoot "native-host.json")
} | ConvertTo-Json -Depth 5
