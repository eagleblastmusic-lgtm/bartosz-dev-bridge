"""Current guidance checks, separate from the frozen docs-only NX-070 gate."""
from pathlib import Path
from bdb_vnext.composition import default_vnext_runtime_root

ROOT = Path(__file__).resolve().parents[1]


def test_readme_runtime_matches_actual_repo_local_resolver():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert default_vnext_runtime_root() == ROOT / "runtime"
    assert "<repo>\\runtime" in readme
    assert "%LOCALAPPDATA%\\BartoszDevBridge-vNext" not in readme
    assert "docs/VNEXT_PRODUCTION_RUNTIME.md" in readme


def test_snapshot_preserves_observation_date_and_requires_fresh_identity():
    snapshot = (ROOT / "docs/NX070_CURRENT_STATE.md").read_text(encoding="utf-8")
    assert "Observation date: `2026-08-27`" in snapshot
    assert "Temporal scope:" in snapshot
    assert "do not establish today's" in snapshot
    assert "ACTIVE slot, client plan or loaded Chrome bytes" in snapshot
    assert "APP_REMEDIATION_IMPLEMENTATION_2026_10_02.md" in snapshot


def test_active_ci_runs_new_properties_and_owning_producers():
    workflow = (ROOT / ".github/workflows/bdb-vnext-ci.yml").read_text(encoding="utf-8")
    for filename in ("test_project_memory_persistence_contract.py", "test_project_memory_live_retention.py", "test_project_workflow_responsiveness.py",
                     "test_browser_admission_retention.py", "test_local_execution_remediation.py", "test_remediation_guidance.py"):
        assert f"tests/{filename}" in workflow
    assert "scripts/run_million_event_harness.py" in workflow
    assert "Execute source-bound" in workflow and "runner.os == 'Windows'" in workflow
