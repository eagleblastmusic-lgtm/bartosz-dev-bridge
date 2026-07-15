[CmdletBinding()]
param(
    [string]$Root = "C:\Projekt\DevMaster\POC0B",
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

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content + [Environment]::NewLine, $encoding)
}

$Git = Require-Command -Name "git"
$Python = Require-Command -Name "python"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RootPath = [System.IO.Path]::GetFullPath($Root)
$VenvPath = Join-Path $RootPath ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$ControlPath = Join-Path $RootPath "control"
$FixturePath = Join-Path $RootPath "bdb-poc0b-fixture"
$WorktreeRoot = Join-Path $RootPath "worktrees"
$ConfigPath = Join-Path $RootPath "poc_config.json"

if (Test-Path $FixturePath) {
    throw "POC-0B fixture already exists at '$FixturePath'. Preserve it as evidence; use a different -Root for a fresh blind run."
}

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

New-Item -ItemType Directory -Force -Path `
    $FixturePath, `
    (Join-Path $FixturePath "src"), `
    (Join-Path $FixturePath "tests") | Out-Null

$HiddenCases = @(
    '    assert normalize_key(" \u0053tra\u00dfe ") == "strasse"',
    '    assert normalize_key(" \uff21lice ") == "alice"',
    '    assert normalize_key("Cafe\u0301") == "caf\u00e9"'
)

$RandomBytes = New-Object byte[] 4
$Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $Rng.GetBytes($RandomBytes)
} finally {
    $Rng.Dispose()
}
$HiddenIndex = [int]([BitConverter]::ToUInt32($RandomBytes, 0) % [uint32]$HiddenCases.Count)
$HiddenAssertion = $HiddenCases[$HiddenIndex]

$GitIgnore = @'
__pycache__/
.pytest_cache/
*.py[cod]
'@

$PyProject = @'
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "bdb-poc0b-fixture"
version = "0.0.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
'@

$Init = @'
"""Synthetic blind-diagnosis fixture for POC-0B."""
'@

$Source = @'
def normalize_key(value: str) -> str:
    """Return a stable key used for case-insensitive matching."""
    return value
'@

$Tests = @"
from src.normalize import normalize_key


def test_trims_and_lowercases_ascii() -> None:
    assert normalize_key("  Alice  ") == "alice"


def test_preserves_internal_spaces() -> None:
    assert normalize_key("Mary Jane") == "mary jane"


def test_contract_edge_case() -> None:
$HiddenAssertion
"@

Write-Utf8NoBom -Path (Join-Path $FixturePath ".gitignore") -Content $GitIgnore
Write-Utf8NoBom -Path (Join-Path $FixturePath "pyproject.toml") -Content $PyProject
Write-Utf8NoBom -Path (Join-Path $FixturePath "src\__init__.py") -Content $Init
Write-Utf8NoBom -Path (Join-Path $FixturePath "src\normalize.py") -Content $Source
Write-Utf8NoBom -Path (Join-Path $FixturePath "tests\test_normalize.py") -Content $Tests

& $Git -C $FixturePath init -b main
& $Git -C $FixturePath config user.name "Bartosz Dev Bridge POC"
& $Git -C $FixturePath config user.email "bdb-poc@users.noreply.github.com"
& $Git -C $FixturePath add -- .gitignore pyproject.toml src tests
& $Git -C $FixturePath commit -m "fixture: establish blind POC-0B baseline"

$FixtureStatus = (& $Git -C $FixturePath status --porcelain=v1)
if ($FixtureStatus) {
    throw "Generated POC-0B fixture source checkout is not clean."
}
$BaseSha = (& $Git -C $FixturePath rev-parse HEAD).Trim()

$Config = [ordered]@{
    schema_version = "1.1"
    control_repo_path = $ControlPath
    fixture_repo_path = $FixturePath
    worktree_root = $WorktreeRoot
    repository_id = "bdb-poc0b-fixture"
    allowed_paths = @("src/normalize.py")
    poll_interval_seconds = 5
    max_poll_seconds = 300
    max_sequence = 3
    test_timeout_seconds = 45
    python_executable = $VenvPython
}
$Config | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $ConfigPath

Write-Host "POC-0B blind fixture bootstrap completed."
Write-Host "Config:       $ConfigPath"
Write-Host "Control repo: $ControlPath"
Write-Host "Fixture repo: $FixturePath"
Write-Host "Base SHA:     $BaseSha"
Write-Host "Worktrees:    $WorktreeRoot"
Write-Host "The hidden edge case was selected locally and was not printed."
Write-Host "No token or secret was written to the repository or config. Git authentication remains external."
