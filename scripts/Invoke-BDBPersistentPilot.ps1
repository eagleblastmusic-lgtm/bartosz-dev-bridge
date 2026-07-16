[CmdletBinding()]
param(
    [string]$Root = "",
    [string]$Python = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) {
    $ProjectPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $ProjectPython) {
        $Python = $ProjectPython
    }
    else {
        $Python = "python"
    }
}

if (-not $Root) {
    $Parent = Split-Path $RepoRoot -Parent
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $Root = Join-Path $Parent "bdb-persistent-pilot-$Stamp"
}

$Runner = Join-Path $RepoRoot "scripts\run_persistent_pilot.py"
Write-Host "Bartosz Dev Bridge persistent operator pilot"
Write-Host "Repository: $RepoRoot"
Write-Host "Python: $Python"
Write-Host "Pilot root: $Root"
Write-Host ""

& $Python $Runner --root $Root --python $Python
exit $LASTEXITCODE
