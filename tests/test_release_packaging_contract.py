from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p13_artifacts_exist() -> None:
    expected = (
        ROOT / "bdb_release" / "__init__.py",
        ROOT / "bdb_release" / "manifest.py",
        ROOT / "scripts" / "build_release_manifest.py",
        ROOT / "packaging" / "windows" / "control_center_entry.py",
        ROOT / "schemas" / "bdb-release-manifest-v1.schema.json",
        ROOT / ".github" / "workflows" / "control-center-release-artifact.yml",
        ROOT / "docs" / "BDB_CONTROL_CENTER_RELEASE_PACKAGING.md",
        ROOT / "docs" / "adr" / "0014-manual-verified-release-artifacts.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing P13 artifact: {path.relative_to(ROOT)}"
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


def test_packaged_entrypoint_and_package_discovery_are_explicit() -> None:
    entry = read(ROOT / "packaging" / "windows" / "control_center_entry.py")
    pyproject = read(ROOT / "pyproject.toml")
    assert "from bdb_gui.app import main" in entry
    assert 'release = ["PyInstaller>=6.14,<7"]' in pyproject
    assert '"bdb_release*"' in pyproject
    assert "--add-data \"scripts;scripts\"" in read(
        ROOT / ".github" / "workflows" / "control-center-release-artifact.yml"
    )
