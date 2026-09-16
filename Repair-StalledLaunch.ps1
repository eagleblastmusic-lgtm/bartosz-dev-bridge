param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [Parameter(Mandatory = $true)]
    [string]$BindingId,

    [string]$RuntimeRoot = (Join-Path $PSScriptRoot "runtime"),

    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$Python = (Get-Command python -ErrorAction Stop).Source
$env:PYTHONPATH = $PSScriptRoot

$Args = @(
    "-m", "bdb_vnext.stalled_launch_recovery",
    "--runtime-root", $RuntimeRoot,
    "--project-id", $ProjectId,
    "--binding-id", $BindingId
)

if ($Apply) {
    $Args += "--apply"
}

& $Python @Args

if ($LASTEXITCODE -ne 0) {
    throw "Stalled launch recovery zakończył się kodem $LASTEXITCODE"
}
