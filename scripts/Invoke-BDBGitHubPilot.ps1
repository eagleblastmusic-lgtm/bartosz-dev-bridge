[CmdletBinding()]
param(
    [string]$ControlUrl = "https://github.com/eagleblastmusic-lgtm/bartosz-dev-bridge-pilot-control-private.git",
    [string]$Root = "",
    [string]$Python = "",
    [switch]$PrepareOnly
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
    $Root = Join-Path $Parent "bdb-github-pilot-$Stamp"
}

$Runner = Join-Path $RepoRoot "scripts\prepare_github_pilot.py"
$Arguments = @(
    $Runner,
    "--root", $Root,
    "--control-url", $ControlUrl,
    "--python", $Python
)
if ($PrepareOnly) {
    $Arguments += "--prepare-only"
}

Write-Host "Bartosz Dev Bridge private GitHub transport pilot"
Write-Host "Repository: $RepoRoot"
Write-Host "Python: $Python"
Write-Host "Control: $ControlUrl"
Write-Host "Pilot root: $Root"
Write-Host "Prepare only: $($PrepareOnly.IsPresent)"
Write-Host ""

& $Python @Arguments
exit $LASTEXITCODE
