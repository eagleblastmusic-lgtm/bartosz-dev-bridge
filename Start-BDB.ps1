param(
    [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
Set-Location -LiteralPath $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $Python) {
    throw "Python environment not found. Activate the project environment and retry."
}

if (-not $RuntimeRoot) {
    $ResolvedRuntime = @(& $Python -m bdb_vnext.runtime_authority)
    if ($LASTEXITCODE -ne 0) {
        throw "BDB runtime authority resolution failed. Control Center was not started."
    }
    $RuntimeRoot = (($ResolvedRuntime | Select-Object -Last 1) -as [string]).Trim()
    if (-not $RuntimeRoot) {
        throw "BDB runtime authority resolution returned an empty path."
    }
}

Write-Host "BDB runtime authority: $RuntimeRoot"
$Arguments = @("-m", "bdb_gui.app", "--runtime-root", $RuntimeRoot)
& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "BDB Control Center exited with code $LASTEXITCODE."
}
