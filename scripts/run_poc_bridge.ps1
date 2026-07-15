[CmdletBinding()]
param(
    [string]$Root = "C:\BartoszDev\POC0",
    [string]$ConfigPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RootPath = [System.IO.Path]::GetFullPath($Root)
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $RootPath "poc_config.json"
}
$VenvPython = Join-Path $RootPath ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    throw "POC virtual environment is missing. Run scripts\bootstrap_windows.ps1 first."
}
if (-not (Test-Path $ConfigPath)) {
    throw "POC config is missing: $ConfigPath"
}

& $VenvPython (Join-Path $RepoRoot "poc_bridge.py") --config $ConfigPath
exit $LASTEXITCODE
