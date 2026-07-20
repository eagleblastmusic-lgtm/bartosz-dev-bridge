param(
    [string]$Python = "python",
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [ValidateRange(60, 300)]
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$pythonPath = (Get-Command $Python).Source
$rootPath = [System.IO.Path]::GetFullPath($Root)

& $pythonPath `
    (Join-Path $PSScriptRoot "real_repository_pilot.py") `
    --python $pythonPath `
    --root $rootPath `
    --timeout $TimeoutSeconds

if ($LASTEXITCODE -ne 0) {
    throw "Real repository pilot failed with exit code $LASTEXITCODE"
}
