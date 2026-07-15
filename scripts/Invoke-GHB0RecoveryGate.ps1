param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Artifacts = Join-Path $Root "artifacts\ghb0-gate"
New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null

function Invoke-Step {
    param([string]$Name, [string[]]$Arguments)
    Write-Host "==> $Name"
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

Push-Location $Root
try {
    Invoke-Step "Compile poc_bridge" @("-m", "py_compile", "poc_bridge.py")
    Invoke-Step "Compile bdb_bridge" @("-c", "import pathlib,py_compile;[py_compile.compile(str(p),doraise=True) for p in pathlib.Path('bdb_bridge').glob('*.py')]")
    Invoke-Step "Compile bdb_poc" @("-c", "import pathlib,py_compile;[py_compile.compile(str(p),doraise=True) for p in pathlib.Path('bdb_poc').glob('*.py')]")
    Invoke-Step "Recovery gate" @("scripts/run_ghb0_recovery_gate.py", "--output", (Join-Path $Artifacts "recovery-gate.json"))
    Invoke-Step "Targeted lifecycle" @("-m", "pytest", "-o", "addopts=", "-q", "tests/test_workspace_lifecycle.py", "tests/test_workspace_lifecycle_recovery.py", "tests/test_workspace_lifecycle_migrations.py", "tests/test_session_finalization.py", "tests/test_workspace_lifecycle_cli.py", "tests/test_workspace_cleanup_safety.py", "tests/test_ghb0_recovery_safety.py")
    Invoke-Step "POC regressions" @("-m", "pytest", "-o", "addopts=", "-q", "tests/test_poc_bridge.py", "tests/test_core_boundaries.py", "tests/test_no_duplicate_tests.py")
    Invoke-Step "Full suite" @("-m", "pytest", "-o", "addopts=", "-q")
}
finally {
    Pop-Location
}
