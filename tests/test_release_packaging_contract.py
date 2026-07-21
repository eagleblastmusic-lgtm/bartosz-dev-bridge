from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_artifacts_exist() -> None:
    expected = (
        ROOT / "bdb_release" / "__init__.py",
        ROOT / "bdb_release" / "manifest.py",
        ROOT / "bdb_gui" / "version.py",
        ROOT / "scripts" / "build_release_manifest.py",
        ROOT / "scripts" / "Invoke-BDBControlCenterArtifactAcceptance.ps1",
        ROOT / "packaging" / "windows" / "control_center_entry.py",
        ROOT / "schemas" / "bdb-release-manifest-v1.schema.json",
        ROOT / ".github" / "workflows" / "control-center-release-artifact.yml",
        ROOT / "docs" / "BDB_CONTROL_CENTER_RELEASE_PACKAGING.md",
        ROOT / "docs" / "CONTROL_CENTER_0.3.0_ACCEPTANCE.md",
        ROOT / "docs" / "PROJECT_CREATOR_0.3.1.md",
        ROOT / "docs" / "adr" / "0014-manual-verified-release-artifacts.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing release artifact: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_release_manifest_schema_disables_automatic_distribution() -> None:
    schema = json.loads(read(ROOT / "schemas" / "bdb-release-manifest-v1.schema.json"))
    assert schema["$id"] == "bdb-release-manifest-v1"
    assert schema["additionalProperties"] is False
    distribution = schema["properties"]["distribution"]
    assert distribution["additionalProperties"] is False
    assert distribution["properties"]["auto_download"] == {"const": False}
    assert distribution["properties"]["auto_install"] == {"const": False}
    assert distribution["properties"]["published_release"] == {"const": False}
    assert schema["properties"]["signature"] == {"type": "null"}


def test_release_module_has_no_network_install_or_process_execution() -> None:
    source = read(ROOT / "bdb_release" / "manifest.py")
    tree = ast.parse(source)
    forbidden_roots = {
        "subprocess", "socket", "urllib", "requests", "http", "aiohttp", "websockets",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".", 1)[0]}
        else:
            continue
        assert forbidden_roots.isdisjoint(roots)
    lowered = source.lower()
    for token in ("pip install", "msiexec", "start-process", "os.startfile", "shell=true"):
        assert token not in lowered


def test_release_workflow_is_manual_and_does_not_publish() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "control-center-release-artifact.yml")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "schedule:" not in workflow
    assert "contents: read" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "build_release_manifest.py create" in workflow
    assert "build_release_manifest.py verify" in workflow
    assert "--headless-smoke" in workflow
    assert "$data.tray_created -ne $false" in workflow
    for forbidden in (
        "actions/create-release",
        "softprops/action-gh-release",
        "gh release create",
        "contents: write",
        "Invoke-WebRequest",
        "curl ",
        "msiexec",
    ):
        assert forbidden not in workflow


def test_release_version_is_aligned_for_0_3_1() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "control-center-release-artifact.yml")
    pyproject = read(ROOT / "pyproject.toml")
    version_module = read(ROOT / "bdb_gui" / "version.py")
    module_manifest = json.loads(read(ROOT / "manifests" / "bartosz-dev-bridge.module.json"))

    assert 'default: "0.3.1"' in workflow
    assert 'version = "0.3.1"' in pyproject
    assert 'APPLICATION_VERSION = "0.3.1"' in version_module
    assert module_manifest["version"] == "0.3.1"
    assert "Validate source identity and acceptance entrypoint" in workflow
    assert "$applicationVersion" in workflow
    assert "$moduleVersion" in workflow
    assert "$projectVersion" in workflow


def test_module_manifest_preserves_closed_operator_mutation_catalog() -> None:
    module_manifest = json.loads(read(ROOT / "manifests" / "bartosz-dev-bridge.module.json"))
    assert "sessions" in module_manifest["operations"]["read"]
    assert module_manifest["operations"]["mutation"] == ["prepare", "start", "stop", "rearm"]
    assert module_manifest["operations"]["arbitrary_shell"] is False
    assert module_manifest["operations"]["auto_merge"] is False
    assert module_manifest["operations"]["auto_deploy"] is False
    assert module_manifest["contracts"]["session_history"] == "bdb-session-history-v1"
    assert module_manifest["contracts"]["repair_correlation"] == "bdb-repair-correlation-v1"
    assert module_manifest["contracts"]["repair_group"] == "bdb-repair-group-v1"
    assert module_manifest["contracts"]["control_center_smoke"] == "bdb-control-center-smoke-v1"
    assert module_manifest["contracts"]["release_manifest"] == "bdb-release-manifest-v1"


def test_release_smoke_waits_for_windowed_process_and_reports_failure_details() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "control-center-release-artifact.yml")
    assert workflow.count("shell: pwsh") == 4
    assert "shell: powershell" not in workflow
    assert "[System.Diagnostics.ProcessStartInfo]::new()" in workflow
    assert "$startInfo.UseShellExecute = $false" in workflow
    assert "$process.WaitForExit()" in workflow
    assert "$smokeExitCode = $process.ExitCode" in workflow
    assert "Test-Path -LiteralPath $report -PathType Leaf" in workflow
    assert 'Write-Host "=== BUNDLED SMOKE REPORT ==="' in workflow
    assert "produced no report" in workflow


def test_release_smoke_proves_control_center_0_3_1_features() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "control-center-release-artifact.yml")
    required_checks = (
        '$data.application_version -ne "${{ inputs.version }}"',
        "$data.operation_flow_present -ne $true",
        "$data.current_operation_read_only -ne $true",
        "$data.history_tabs_present -ne $true",
        "$data.session_history_view_present -ne $true",
        "$data.session_history_read_only -ne $true",
        "$data.session_result_open_explicit -ne $true",
        "$data.session_receipt_open_explicit -ne $true",
        "$data.session_folder_open_explicit -ne $true",
        "$data.session_repair_relationships_inferred -ne $false",
        "$data.projects_wizard_present -ne $true",
        "$data.project_creator_button_present -ne $true",
        "$data.project_creator_worker_active -ne $false",
    )
    for check in required_checks:
        assert check in workflow
    assert "Bundled smoke violates the Control Center 0.3.1 contract" in workflow


def test_release_manifest_is_rechecked_against_requested_identity() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "control-center-release-artifact.yml")
    assert '$manifest.version -ne "${{ inputs.version }}"' in workflow
    assert '$manifest.source_commit -ne "$env:GITHUB_SHA"' in workflow
    assert "$manifest.distribution.auto_download -ne $false" in workflow
    assert "$manifest.distribution.auto_install -ne $false" in workflow
    assert "$manifest.distribution.published_release -ne $false" in workflow
    assert "$null -ne $manifest.signature" in workflow


def test_self_contained_acceptance_is_copied_invoked_and_uploaded() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "control-center-release-artifact.yml")
    assert "Invoke-BDBControlCenterArtifactAcceptance.ps1" in workflow
    assert "Copy-Item -LiteralPath" in workflow
    assert '-ExpectedVersion "${{ inputs.version }}"' in workflow
    assert '-ExpectedSourceCommit "$env:GITHUB_SHA"' in workflow
    assert "Self-contained artifact acceptance failed" in workflow
    assert "bdb-control-center-acceptance-v1.json" in read(
        ROOT / "scripts" / "Invoke-BDBControlCenterArtifactAcceptance.ps1"
    )


def test_acceptance_script_is_local_non_installing_and_bounded() -> None:
    script = read(ROOT / "scripts" / "Invoke-BDBControlCenterArtifactAcceptance.ps1")
    lowered = script.lower()
    required = (
        "get-filehash",
        "expand-archive",
        "processstartinfo",
        "--headless-smoke",
        "mutation_operations_invoked",
        "operation_flow_present",
        "session_history_view_present",
        "session_repair_relationships_inferred",
        "remove-item",
    )
    for token in required:
        assert token in lowered
    for forbidden in (
        "invoke-webrequest",
        "start-bitstransfer",
        "curl ",
        "wget ",
        "msiexec",
        "winget",
        "choco",
        "set-executionpolicy -scope localmachine",
        "runas",
        "start-process -verb runas",
    ):
        assert forbidden not in lowered


def test_packaged_entrypoint_and_package_discovery_are_explicit() -> None:
    entry = read(ROOT / "packaging" / "windows" / "control_center_entry.py")
    pyproject = read(ROOT / "pyproject.toml")
    assert "from bdb_gui.app import main" in entry
    assert 'release = ["PyInstaller>=6.14,<7"]' in pyproject
    assert '"bdb_release*"' in pyproject
    assert '"bdb_gui*"' in pyproject
    assert '--add-data "scripts;scripts"' in read(
        ROOT / ".github" / "workflows" / "control-center-release-artifact.yml"
    )
