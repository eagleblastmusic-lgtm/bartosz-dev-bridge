[CmdletBinding()]
param(
    [string]$Python = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) {
    $ProjectPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $ProjectPython) {
        $Python = $ProjectPython
    }
    else {
        $Python = "python"
    }
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host ""
    Write-Host "==> $Name"
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

Push-Location $Root
try {
    Write-Host "Bartosz Dev Bridge local E2E POC"
    Write-Host "Repository: $Root"
    Write-Host "Python: $Python"

    Invoke-Step "Python version" @("--version")
    Invoke-Step "Compile Bridge" @(
        "-c",
        "import pathlib,py_compile;[py_compile.compile(str(p),doraise=True) for p in pathlib.Path('bdb_bridge').glob('*.py')]"
    )
    Invoke-Step "Legacy Git transport end-to-end" @(
        "-m", "pytest", "-o", "addopts=", "-q",
        "tests/test_poc_bridge.py::test_end_to_end_local_transport"
    )
    Invoke-Step "Final multi-file editing gate" @(
        "-m", "pytest", "-o", "addopts=", "-q",
        "tests/test_ghb2d_final_editing_gate.py"
    )
    Invoke-Step "Durable multi-file recovery" @(
        "-m", "pytest", "-o", "addopts=", "-q",
        "tests/test_multi_file_patch_recovery.py"
    )
    Invoke-Step "Foreground service lifecycle" @(
        "-m", "pytest", "-o", "addopts=", "-q",
        "tests/test_service_cli_lifecycle.py"
    )
    Invoke-Step "Working tree whitespace check" @(
        "-c",
        "import subprocess,sys;sys.exit(subprocess.run(['git','diff','--check'],shell=False).returncode)"
    )

    Write-Host ""
    Write-Host "LOCAL E2E POC: PASS"
    Write-Host "All repositories, worktrees, journals and remotes used by the gate were synthetic and isolated in pytest temporary directories."
}
finally {
    Pop-Location
}
