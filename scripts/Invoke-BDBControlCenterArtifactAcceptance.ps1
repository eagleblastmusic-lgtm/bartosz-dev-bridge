[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseDirectory,

    [string]$ExpectedVersion = "0.3.1",

    [string]$ExpectedSourceCommit = "",

    [switch]$KeepExtracted
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$release = (Resolve-Path -LiteralPath $ReleaseDirectory).Path
$manifestPath = Join-Path $release "bdb-release-manifest-v1.json"
$receiptPath = Join-Path $release "bdb-control-center-acceptance-v1.json"
$extractRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("bdb-control-center-acceptance-" + [guid]::NewGuid().ToString("N"))

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

try {
    Assert-Condition (Test-Path -LiteralPath $manifestPath -PathType Leaf) "Release manifest is missing"
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

    Assert-Condition ($manifest.schema -eq "bdb-release-manifest-v1") "Unsupported release manifest schema"
    Assert-Condition ($manifest.product -eq "BDB Control Center") "Unexpected release product"
    Assert-Condition ($manifest.version -eq $ExpectedVersion) "Release version does not match the expected version"
    Assert-Condition ($manifest.channel -eq "manual-artifact") "Release channel must remain manual-artifact"
    Assert-Condition ($manifest.platform -eq "windows-x86_64") "Release platform must be windows-x86_64"
    Assert-Condition ($manifest.entrypoint -eq "BDB-Control-Center.exe") "Unexpected release entrypoint"
    Assert-Condition ($manifest.source_commit -match "^[0-9a-f]{40}$") "Release source commit is invalid"
    if ($ExpectedSourceCommit) {
        Assert-Condition ($manifest.source_commit -eq $ExpectedSourceCommit) "Release source commit does not match the expected commit"
    }
    Assert-Condition ($manifest.distribution.auto_download -eq $false) "Automatic download must remain disabled"
    Assert-Condition ($manifest.distribution.auto_install -eq $false) "Automatic install must remain disabled"
    Assert-Condition ($manifest.distribution.published_release -eq $false) "Published release flag must remain disabled"
    Assert-Condition ($null -eq $manifest.signature) "Unsigned artifact must not claim a signature"

    $zipPath = Join-Path $release $manifest.artifact.name
    Assert-Condition (Test-Path -LiteralPath $zipPath -PathType Leaf) "Release ZIP is missing"
    $zipItem = Get-Item -LiteralPath $zipPath
    Assert-Condition ($zipItem.Name -eq $manifest.artifact.name) "Release ZIP name differs from the manifest"
    Assert-Condition ($zipItem.Length -eq [int64]$manifest.artifact.size_bytes) "Release ZIP size differs from the manifest"
    $zipSha256 = "sha256:" + (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Condition ($zipSha256 -eq $manifest.artifact.sha256) "Release ZIP SHA-256 differs from the manifest"

    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
    $executable = Join-Path $extractRoot "BDB-Control-Center\BDB-Control-Center.exe"
    Assert-Condition (Test-Path -LiteralPath $executable -PathType Leaf) "Bundled Control Center executable is missing"

    $workspaces = Join-Path $extractRoot "acceptance-workspaces"
    $smokePath = Join-Path $extractRoot "bdb-control-center-smoke.json"
    New-Item -ItemType Directory -Path $workspaces -Force | Out-Null

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $executable
    $startInfo.UseShellExecute = $false
    foreach ($argument in @(
        "--workspaces-root",
        $workspaces,
        "--headless-smoke",
        "--json-out",
        $smokePath
    )) {
        [void]$startInfo.ArgumentList.Add($argument)
    }

    $process = [System.Diagnostics.Process]::Start($startInfo)
    Assert-Condition ($null -ne $process) "Bundled Control Center process did not start"
    $process.WaitForExit()
    Assert-Condition ($process.ExitCode -eq 0) "Bundled Control Center smoke returned a non-zero exit code"
    Assert-Condition (Test-Path -LiteralPath $smokePath -PathType Leaf) "Bundled Control Center smoke report is missing"

    $smoke = Get-Content -LiteralPath $smokePath -Raw | ConvertFrom-Json
    Assert-Condition ($smoke.schema -eq "bdb-control-center-smoke-v1") "Unsupported Control Center smoke schema"
    Assert-Condition ($smoke.status -eq "success") "Bundled Control Center smoke did not pass"
    Assert-Condition ($smoke.application_version -eq $ExpectedVersion) "Bundled application version differs from the expected version"
    Assert-Condition ($smoke.read_only_startup -eq $true) "Bundled startup was not read-only"
    Assert-Condition ($smoke.mutation_operations_invoked -eq 0) "Bundled smoke invoked a mutation"
    Assert-Condition ($smoke.tray_created -eq $false) "Headless smoke unexpectedly created the tray"
    Assert-Condition ($smoke.operation_flow_present -eq $true) "Operation flow panel is missing"
    Assert-Condition ($smoke.current_operation_read_only -eq $true) "Current operation view is not read-only"
    Assert-Condition ($smoke.history_tabs_present -eq $true) "History tabs are missing"
    Assert-Condition ($smoke.session_history_view_present -eq $true) "Session history view is missing"
    Assert-Condition ($smoke.session_history_read_only -eq $true) "Session history is not read-only"
    Assert-Condition ($smoke.session_result_open_explicit -eq $true) "Result opening is not explicit"
    Assert-Condition ($smoke.session_receipt_open_explicit -eq $true) "Receipt opening is not explicit"
    Assert-Condition ($smoke.session_folder_open_explicit -eq $true) "Folder opening is not explicit"
    Assert-Condition ($smoke.session_repair_relationships_inferred -eq $false) "Repair relationships were inferred"
    Assert-Condition ($smoke.projects_wizard_present -eq $true) "Projects wizard is missing"
    Assert-Condition ($smoke.project_creator_button_present -eq $true) "Project Creator button is missing"
    Assert-Condition ($smoke.project_creator_worker_active -eq $false) "Project Creator worker ran during read-only smoke"

    $receipt = [ordered]@{
        schema = "bdb-control-center-acceptance-v1"
        status = "pass"
        version = $manifest.version
        source_commit = $manifest.source_commit
        artifact_name = $manifest.artifact.name
        artifact_size_bytes = [int64]$manifest.artifact.size_bytes
        artifact_sha256 = $zipSha256
        application_version = $smoke.application_version
        read_only_startup = $smoke.read_only_startup
        mutation_operations_invoked = $smoke.mutation_operations_invoked
        operation_flow_present = $smoke.operation_flow_present
        session_history_view_present = $smoke.session_history_view_present
        session_repair_relationships_inferred = $smoke.session_repair_relationships_inferred
        project_creator_button_present = $smoke.project_creator_button_present
        accepted_at = [datetime]::UtcNow.ToString("o")
        extracted_path = if ($KeepExtracted) { $extractRoot } else { $null }
    }
    $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding utf8
    $receipt | ConvertTo-Json -Depth 8
}
finally {
    if (-not $KeepExtracted -and (Test-Path -LiteralPath $extractRoot)) {
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
}
