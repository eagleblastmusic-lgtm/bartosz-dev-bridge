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
$ExitCode = $LASTEXITCODE

if ($ExitCode -ne 0) {
    $QueuePath = Join-Path $RuntimeRoot "control\project-launch-queue.json"
    $LockPath  = "$QueuePath.lock"

    Write-Host ""
    Write-Host "=== QUEUE LOCK DIAGNOSTICS ===" -ForegroundColor Yellow
    Write-Host "Lock path: $LockPath"
    Write-Host "Exists:    $(Test-Path -LiteralPath $LockPath)"

    if (Test-Path -LiteralPath $LockPath) {
        try {
            $Item = Get-Item -LiteralPath $LockPath
            Write-Host "Created UTC:    $($Item.CreationTimeUtc.ToString('o'))"
            Write-Host "Last write UTC: $($Item.LastWriteTimeUtc.ToString('o'))"

            $Raw = Get-Content -LiteralPath $LockPath -Raw
            Write-Host ""
            Write-Host "Lock metadata:"
            Write-Host $Raw

            try {
                $Lock = $Raw | ConvertFrom-Json
                if ($null -ne $Lock.pid) {
                    $Owner = Get-Process -Id ([int]$Lock.pid) -ErrorAction SilentlyContinue
                    Write-Host ""
                    Write-Host "Owner PID:  $($Lock.pid)"
                    Write-Host "Owner alive: $($null -ne $Owner)"
                    if ($null -ne $Owner) {
                        Write-Host "Owner process: $($Owner.ProcessName)"
                        Write-Host "Owner started: $($Owner.StartTime.ToUniversalTime().ToString('o'))"
                    }
                }
            }
            catch {
                Write-Host "Nie udało się sparsować metadanych locka jako JSON."
            }
        }
        catch {
            Write-Host "Nie udało się odczytać locka: $($_.Exception.Message)"
        }
    }

    throw "Stalled launch recovery zakończył się kodem $ExitCode"
}
