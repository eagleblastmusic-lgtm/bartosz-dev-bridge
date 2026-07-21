[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$DistRoot = "",
    [string]$WorkRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "The windowless Native Host build supports Windows only"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExecutable = (Get-Command $Python -ErrorAction Stop).Source
$entryPoint = Join-Path $repoRoot "packaging\windows\native_host_entry.py"
if (-not (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
    throw "Missing Native Host entry point: $entryPoint"
}

if ([string]::IsNullOrWhiteSpace($DistRoot)) {
    $DistRoot = Join-Path $repoRoot "dist-native-host"
}
if ([string]::IsNullOrWhiteSpace($WorkRoot)) {
    $WorkRoot = Join-Path $repoRoot "build-native-host"
}
$distPath = [System.IO.Path]::GetFullPath($DistRoot)
$workPath = [System.IO.Path]::GetFullPath($WorkRoot)
$specPath = Join-Path $workPath "spec"
New-Item -ItemType Directory -Path $distPath -Force | Out-Null
New-Item -ItemType Directory -Path $workPath -Force | Out-Null
New-Item -ItemType Directory -Path $specPath -Force | Out-Null

& $pythonExecutable -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name BDB-Native-Host `
    --paths $repoRoot `
    --collect-submodules bdb_bridge `
    --distpath $distPath `
    --workpath $workPath `
    --specpath $specPath `
    $entryPoint
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller windowless Native Host build failed with exit code $LASTEXITCODE"
}

$hostDirectory = Join-Path $distPath "BDB-Native-Host"
$hostExecutable = Join-Path $hostDirectory "BDB-Native-Host.exe"
if (-not (Test-Path -LiteralPath $hostExecutable -PathType Leaf)) {
    throw "Windowless Native Host executable was not created: $hostExecutable"
}

[ordered]@{
    status = "built"
    executable = (Resolve-Path -LiteralPath $hostExecutable).Path
    directory = (Resolve-Path -LiteralPath $hostDirectory).Path
    python = $pythonExecutable
    entry_point = $entryPoint
} | ConvertTo-Json -Depth 5
