from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_local_browser_benchmark.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bdb_local_browser_benchmark", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def successful_run(module, suite: str, index: int, value: float) -> dict[str, object]:
    return {
        "suite": suite,
        "index": index,
        "status": "success",
        "native_roundtrip_ms": value,
        "cold_start_to_running_ms": value + 10.0 if suite == module.SUITE_COLD else None,
        "cold_total_ms": value + 20.0 if suite == module.SUITE_COLD else None,
        "timing": {
            "durations_ms": {
                "end_to_end_ms": value - 10.0,
                "execution_ms": value / 2.0,
                "result_publication_ms": 25.0,
            }
        },
    }


def test_benchmark_script_is_valid_and_has_narrow_safety_contract() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    ast.parse(source)
    assert 'PILOT_REPOSITORY_ID = "bdb-local-browser-pilot"' in source
    assert 'PILOT_ALIAS = "pilot"' in source
    assert 'PILOT_ALLOWED_PATHS = ["src/clamp.py", "tests/test_clamp.py", "PILOT_RESULT.md"]' in source
    assert "Benchmark requires Bridge OFFLINE at start" in source
    assert "require_clean_checkout(paths.fixture_repo" in source
    assert '"action": "submit_action"' in source
    assert '"action": "result"' in source
    assert "encode_native_message" in source
    assert "read_native_message" in source
    assert "build_command_timing" in source
    for forbidden in (
        "git clean",
        "reset --hard",
        "shutil.rmtree",
        "Remove-Item",
        "gicleeart",
        "eval(",
        "shell=True",
    ):
        assert forbidden not in source


def test_nearest_rank_and_metric_summary_are_deterministic() -> None:
    module = load_module()
    values = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert module.nearest_rank(values, 50.0) == 3.0
    assert module.nearest_rank(values, 95.0) == 5.0
    assert module.metric_summary(values) == {
        "count": 5,
        "min": 1.0,
        "mean": 3.0,
        "p50": 3.0,
        "p95": 5.0,
        "max": 5.0,
    }
    assert module.metric_summary([])["p95"] is None


def test_summary_separates_suites_and_nested_journal_metrics() -> None:
    module = load_module()
    runs = [
        successful_run(module, module.SUITE_OPEN_READ, 1, 100.0),
        successful_run(module, module.SUITE_OPEN_READ, 2, 200.0),
        successful_run(module, module.SUITE_PATCH, 1, 900.0),
        successful_run(module, module.SUITE_COLD, 1, 1200.0),
        {
            "suite": module.SUITE_PATCH,
            "index": 2,
            "status": "failed",
            "error": "synthetic",
        },
    ]
    summary = module.summarize_runs(runs)
    assert summary[module.SUITE_OPEN_READ]["runs"] == 2
    assert summary[module.SUITE_OPEN_READ]["successes"] == 2
    assert summary[module.SUITE_OPEN_READ]["metrics_ms"]["native_roundtrip_ms"]["p50"] == 100.0
    assert summary[module.SUITE_PATCH]["failures"] == 1
    assert summary[module.SUITE_PATCH]["metrics_ms"]["execution_ms"]["p95"] == 450.0
    assert summary[module.SUITE_COLD]["metrics_ms"]["cold_total_ms"]["p95"] == 1220.0
    assert summary["all_successful_runs"]["metrics_ms"]["result_publication_ms"]["count"] == 4


def test_target_evaluation_and_markdown_report() -> None:
    module = load_module()
    runs = [
        successful_run(module, module.SUITE_OPEN_READ, 1, 500.0),
        successful_run(module, module.SUITE_PATCH, 1, 1500.0),
        successful_run(module, module.SUITE_COLD, 1, 2500.0),
    ]
    summary = module.summarize_runs(runs)
    targets = module.evaluate_targets(summary)
    assert targets
    assert all(item["met"] for item in targets)
    report = {
        "status": "pass",
        "started_at": "2026-07-17T00:00:00Z",
        "finished_at": "2026-07-17T00:01:00Z",
        "environment": {
            "implementation_head": "a" * 40,
            "repository_id": module.PILOT_REPOSITORY_ID,
        },
        "summary": summary,
        "targets": targets,
        "runs": runs,
    }
    markdown = module.markdown_report(report)
    assert "local performance benchmark" in markdown
    assert "Native Host" in markdown
    assert "does **not** measure ChatGPT answer generation" in markdown
    assert "| `warm_open_read` |" in markdown
    assert "| YES |" in markdown
