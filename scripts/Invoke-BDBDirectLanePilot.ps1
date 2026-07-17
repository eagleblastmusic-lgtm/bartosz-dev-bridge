param(
    [string]$Python = "python",
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [ValidateRange(30, 300)]
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
$pythonPath = (Get-Command $Python).Source
$rootPath = [System.IO.Path]::GetFullPath($Root)

& $pythonPath `
    (Join-Path $PSScriptRoot "run_direct_lane_pilot.py") `
    --python $pythonPath `
    --root $rootPath `
    --timeout $TimeoutSeconds

if ($LASTEXITCODE -ne 0) {
    throw "Direct Lane pilot failed with exit code $LASTEXITCODE"
}
