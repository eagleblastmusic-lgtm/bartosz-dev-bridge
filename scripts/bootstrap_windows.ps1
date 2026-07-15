[CmdletBinding()]
param(
    [string]$Root = "C:\Projekt\DevMaster\POC0",
    [string]$ControlRemote = "https://github.com/eagleblastmusic-lgtm/bartosz-dev-poc-control.git"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Required command '$Name' was not found in PATH."
    }
    return $command.Source
}

$Git = Require-Command -Name "git"
$Python = Require-Command -Name "python"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RootPath = [System.IO.Path]::GetFullPath($Root)
$VenvPath = Join-Path $RootPath ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$ControlPath = Join-Path $RootPath "control"
$FixturePath = Join-Path $RootPath "bdb-poc-fixture"
$WorktreeRoot = Join-Path $RootPath "worktrees"
$ConfigPath = Join-Path $RootPath "poc_config.json"

New-Item -ItemType Directory -Force -Path $RootPath, $WorktreeRoot | Out-Null

if (-not (Test-Path $VenvPython)) {
    & $Python -m venv $VenvPath
}
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
& $VenvPython -m pip install --disable-pip-version-check -e "$RepoRoot[dev]"

if (-not (Test-Path $ControlPath)) {
    & $Git clone --branch main $ControlRemote $ControlPath
} elseif (-not (Test-Path (Join-Path $ControlPath ".git"))) {
    throw "Control path exists but is not a Git repository: $ControlPath"
}

$ActualRemote = (& $Git -C $ControlPath remote get-url origin).Trim()
if ($ActualRemote -ne $ControlRemote) {
    throw "Control repository remote mismatch. Expected '$ControlRemote', got '$ActualRemote'."
}
& $Git -C $ControlPath fetch --prune origin `
    "+refs/heads/commands:refs/remotes/origin/commands" `
    "+refs/heads/results:refs/remotes/origin/results"
& $Git -C $ControlPath show-ref --verify --quiet refs/remotes/origin/commands
if ($LASTEXITCODE -ne 0) { throw "Remote branch commands is unavailable." }
& $Git -C $ControlPath show-ref --verify --quiet refs/remotes/origin/results
if ($LASTEXITCODE -ne 0) { throw "Remote branch results is unavailable." }

if (-not (Test-Path $FixturePath)) {
    Copy-Item -Recurse -Path (Join-Path $RepoRoot "bdb-poc-fixture") -Destination $FixturePath
    & $Git -C $FixturePath init -b main
    & $Git -C $FixturePath config user.name "Bartosz Dev Bridge POC"
    & $Git -C $FixturePath config user.email "bdb-poc@users.noreply.github.com"
    & $Git -C $FixturePath add -- .gitignore pyproject.toml src tests
    & $Git -C $FixturePath commit -m "fixture: establish POC-0 baseline"
} elseif (-not (Test-Path (Join-Path $FixturePath ".git"))) {
    throw "Fixture path exists but is not a Git repository: $FixturePath"
}

$FixtureStatus = (& $Git -C $FixturePath status --porcelain=v1)
if ($FixtureStatus) {
    throw "Fixture source checkout is not clean. Preserve or remove local changes before continuing."
}
$BaseSha = (& $Git -C $FixturePath rev-parse HEAD).Trim()

$Config = [ordered]@{
    schema_version = "1.1"
    control_repo_path = $ControlPath
    fixture_repo_path = $FixturePath
    worktree_root = $WorktreeRoot
    repository_id = "bdb-poc-fixture"
    allowed_paths = @("src/clamp.py", "tests/test_clamp.py")
    poll_interval_seconds = 5
    max_poll_seconds = 300
    max_sequence = 3
    test_timeout_seconds = 45
    python_executable = $VenvPython
}
$Config | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $ConfigPath

Write-Host "POC-0 bootstrap completed."
Write-Host "Config:       $ConfigPath"
Write-Host "Control repo: $ControlPath"
Write-Host "Fixture repo: $FixturePath"
Write-Host "Base SHA:     $BaseSha"
Write-Host "Worktrees:    $WorktreeRoot"
Write-Host "No token or secret was written to the repository or config. Git authentication remains external."
