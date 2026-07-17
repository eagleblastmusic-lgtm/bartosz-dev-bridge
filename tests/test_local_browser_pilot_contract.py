from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_preparer_is_valid_python_and_uses_canonical_patch_bytes() -> None:
    source = read("scripts/prepare_local_browser_pilot.py")
    ast.parse(source)
    assert 'ALIAS = "pilot"' in source
    assert 'REPOSITORY_ID = "bdb-local-browser-pilot"' in source
    assert '"content_base64"' in source
    assert '"content_sha256"' in source
    assert '"content_encoding"' not in source
    assert 'shell=False' in source


def test_preparer_has_an_exact_three_path_allowlist() -> None:
    source = read("scripts/prepare_local_browser_pilot.py")
    assert 'ALLOWED_PATHS = ["src/clamp.py", "tests/test_clamp.py", "PILOT_RESULT.md"]' in source
    assert '"allowed_paths": ALLOWED_PATHS' in source
    assert "gicleeart" not in source.lower()
    assert "bartosz-dev-poc-control" not in source.lower()


def test_preparer_writes_read_only_and_mutating_actions() -> None:
    source = read("scripts/prepare_local_browser_pilot.py")
    assert '"operation": "open_read"' in source
    assert '"operation": "multi_file_patch"' in source
    assert '"profile_id": "poc_pytest"' in source
    assert '"expected_revision": 0' in source
    assert '"path": "src/clamp.py"' in source
    assert '"path": "PILOT_RESULT.md"' in source


def test_powershell_bootstrap_refuses_existing_native_registration() -> None:
    source = read("scripts/Invoke-BDBLocalBrowserPilot.ps1")
    assert 'Existing Native Host installation detected and will not be overwritten' in source
    assert 'Existing Native Host registry entry detected and will not be overwritten' in source
    assert 'Bridge checkout must be clean before pilot setup' in source
    assert 'RepositoryAlias = "pilot"' in source
    assert '"Setup", "Status", "Stop"' in source


def test_powershell_bootstrap_never_deletes_or_broadens_scope() -> None:
    source = read("scripts/Invoke-BDBLocalBrowserPilot.ps1")
    forbidden = (
        "Remove-Item -Recurse",
        "git clean",
        "reset --hard",
        "rmdir /s",
        "shutil.rmtree",
        "RepositoryAlias = \"gicleeart\"",
    )
    for token in forbidden:
        assert token not in source
    assert 'artifacts_preserved = $true' in source
    assert 'native_registration_preserved = $true' in source
    assert 'Add-Member -NotePropertyName stopped_at' in source
    assert 'Wait-ForBridgeState $pythonExecutable $bridgeConfig "OFFLINE"' in source


def test_runbook_requires_assisted_read_only_before_mutation() -> None:
    docs = read("docs/LOCAL_BROWSER_PILOT.md")
    load_index = docs.index("## 1. Load the unpacked extension")
    setup_index = docs.index("## 2. Prepare and start")
    read_index = docs.index("## 4. First browser action — read only")
    mutate_index = docs.index("## 5. Second browser action — isolated edit and pytest")
    assert load_index < setup_index < read_index < mutate_index
    assert "Do not enable browser AUTO yet" in docs
    assert "It is not authorization to attach a business repository" in docs


def test_manifest_remains_narrow_for_the_operator_pilot() -> None:
    manifest = json.loads(read("browser_extension/manifest.json"))
    assert manifest["permissions"] == ["nativeMessaging", "storage"]
    assert manifest["host_permissions"] == ["https://chatgpt.com/*"]
