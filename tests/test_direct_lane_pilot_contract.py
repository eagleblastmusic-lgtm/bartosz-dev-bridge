from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_ci_runs_direct_lane_native_pilot_on_windows_314() -> None:
    workflow = read(".github/workflows/bridge-ci.yml")
    assert "Run Direct Lane Native pilot" in workflow
    assert "runner.os == 'Windows' && matrix.python-version == '3.14'" in workflow
    assert "Invoke-BDBDirectLanePilot.ps1" in workflow
    assert "run_direct_lane_pilot_checked.py" in workflow


def test_checked_runner_executes_native_host_and_normalizes_only_known_stop_messages() -> None:
    checked = read("scripts/run_direct_lane_pilot_checked.py")
    assert 'from bdb_bridge.native_host import main' in checked
    assert "sys.argv = ['bdb-native-host', *sys.argv[1:]]" in checked
    assert "_STOP_MESSAGES = frozenset" in checked
    assert "Graceful stop request sent successfully." in checked
    assert "return {\"message\": text}" in checked


def test_checked_runner_emits_canonical_multi_file_patch_content() -> None:
    checked = read("scripts/run_direct_lane_pilot_checked.py")
    assert '"content_base64": base64.b64encode(content).decode("ascii")' in checked
    assert '"content_sha256": pilot.sha256_value(content)' in checked
    assert "pilot.content_fields = _canonical_content_fields" in checked
    assert '"content_encoding"' not in checked


def test_checked_runner_preserves_exact_fixture_bytes_on_windows() -> None:
    checked = read("scripts/run_direct_lane_pilot_checked.py")
    assert 'pilot.git(fixture, "config", "core.autocrlf", "false")' in checked
    assert "pilot.initialize_fixture = _checked_initialize_fixture" in checked


def test_checked_runner_avoids_fresh_journal_status_race() -> None:
    checked = read("scripts/run_direct_lane_pilot_checked.py")
    assert 'if description == "Bridge RUNNING":' in checked
    assert "time.sleep(1.0)" in checked
    assert "pilot.wait_until = _checked_wait_until" in checked


def test_checked_runner_requires_exact_offline_to_published_transition() -> None:
    checked = read("scripts/run_direct_lane_pilot_checked.py")
    assert 'before.get("command_state") != "result_staged"' in checked
    assert 'after.get("command_state") != "result_published"' in checked
    assert 'report.get("local_result_before_git_restore") is not True' in checked
    assert 'report.get("git_fallback_published_without_reexecution") is not True' in checked
    assert 'report["checked_runner_validated"] = True' in checked


def test_pilot_takes_git_offline_before_native_submission() -> None:
    pilot = read("scripts/run_direct_lane_pilot.py")
    offline = pilot.index("remote.rename(offline_remote)")
    native = pilot.index("native = run(")
    restore = pilot.index("offline_remote.rename(remote)")
    assert offline < native < restore
    assert 'report["local_result_before_git_restore"] = True' in pilot
    assert '"git_fallback_published_without_reexecution": True' in pilot
