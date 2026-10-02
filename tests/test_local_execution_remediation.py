"""Actual policy, raw evidence and Windows containment regressions."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys

import pytest

from bdb_vnext.execution_policy import ExecutionPolicyEvaluator, PolicyEffectClass
from bdb_vnext.local_execution_contract import LocalExecutionRequest, ExecutionEffectClass, LocalExecutionContractError, MechanicalExecutionStatus
from bdb_vnext.tool_adapters import classified_request
from bdb_vnext.output_cancellation_hardening import HardenedOutputEvidenceFactory
from bdb_vnext import stateless_process_runner as runner_module


def request(argv, **kwargs):
    return LocalExecutionRequest(execution_id="remediation", project_id="p1", argv=tuple(argv), expected_source_head="a" * 40, expected_source_tree="b" * 40, **kwargs)


@pytest.mark.parametrize("argv,env,adapter", [
    ((sys.executable, "-c", "open('owned','w').write('x')"), {}, "tool.python"),
    (("git", "--no-pager", "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", "--output=owned"), {}, "process.raw"),
    (("git", "status"), {"GIT_CONFIG_COUNT": "1"}, "process.raw"),
    ((sys.executable, "--version"), {"PYTHONPATH": "."}, "tool.python"),
    ((sys.executable, "--version"), {}, "tool.npm"),
    (("git", "--no-pager", "-c", "core.fsmonitor=false", "log", "--no-ext-diff", "--no-textconv", "--exec=owned"), {}, "process.raw"),
])
def test_declared_read_only_cannot_override_argv_environment_or_adapter(tmp_path, argv, env, adapter):
    decision = ExecutionPolicyEvaluator().evaluate(request(argv, env_vars=env, adapter_id=adapter), tmp_path)
    assert decision.decision == "DENY"


def test_absolute_fake_executable_with_known_basename_is_not_trusted(tmp_path):
    fake = tmp_path / Path(sys.executable).name
    fake.write_bytes(b"not installed Python")
    decision = ExecutionPolicyEvaluator().evaluate(request((str(fake), "--version"), adapter_id="tool.python"), tmp_path)
    assert decision.reason_code == "DENY_UNTRUSTED_EXECUTABLE"


def test_derived_paths_are_relative_to_execution_cwd(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    argv = ("git", "--no-pager", "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", "--", "../outside")
    decision = ExecutionPolicyEvaluator().evaluate(request(argv, cwd=str(root)), root)
    assert decision.reason_code == "DENY_TARGET_OUTSIDE_PROJECT"


@pytest.mark.parametrize("env", [{1: "bad"}, {"NAME": 1}, {"NAME": "a\x00b"}])
def test_environment_is_typed_before_digest_or_spawn(env):
    with pytest.raises(LocalExecutionContractError) as caught:
        request((sys.executable, "--version"), env_vars=env)
    assert caught.value.code == "invalid_env_vars"


def approved(req, root):
    evaluator = ExecutionPolicyEvaluator()
    token = evaluator.approval_registry.issue(req, effect_class=PolicyEffectClass.DESTRUCTIVE, validity_seconds=60)
    decision = evaluator.evaluate(req, root, approval_token=token, current_head="a" * 40, current_tree="b" * 40)
    assert decision.decision == "ALLOW"
    return decision


def test_real_dual_large_streams_have_complete_artifacts_and_bounded_preview(tmp_path, monkeypatch):
    sizes = []
    original = HardenedOutputEvidenceFactory.capture_stream.__func__
    def capture(cls, stream, root):
        result = original(cls, stream, root)
        append = result.append
        def measured(chunk):
            append(chunk)
            sizes.append(len(result.preview))
        result.append = measured
        return result
    monkeypatch.setattr(HardenedOutputEvidenceFactory, "capture_stream", classmethod(capture))
    script = "import os; [(os.write(1,b'a'*8192),os.write(2,b'b'*8192)) for _ in range(512)]"
    req = classified_request(execution_id="large", project_id="p1", argv=(sys.executable, "-I", "-S", "-c", script), expected_source_head="a"*40, expected_source_tree="b"*40)
    result = runner_module.StatelessWindowsProcessRunner().run(req, approved(req, tmp_path), "a"*40, "b"*40, tmp_path)
    assert result.status is MechanicalExecutionStatus.COMPLETED and result.exit_code == 0
    assert max(sizes) <= 64 * 1024
    for evidence, byte in ((result.stdout, b"a"), (result.stderr, b"b")):
        artifact = tmp_path / "evidence" / (evidence.content_digest.split(":")[1] + ".bin")
        expected = byte * (4 * 1024 * 1024)
        assert artifact.read_bytes() == expected
        assert evidence.content_digest == "sha256:" + hashlib.sha256(expected).hexdigest()
        assert HardenedOutputEvidenceFactory.verify_external_artifact_integrity(evidence, tmp_path)
        artifact.write_bytes(b"tampered")
        assert not HardenedOutputEvidenceFactory.verify_external_artifact_integrity(evidence, tmp_path)
        artifact.unlink()
        assert not HardenedOutputEvidenceFactory.verify_external_artifact_integrity(evidence, tmp_path)


def test_artifact_write_or_readback_failure_cannot_be_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(HardenedOutputEvidenceFactory, "verify_external_artifact_integrity", lambda *args: False)
    capture = HardenedOutputEvidenceFactory.capture_stream("stdout", tmp_path)
    capture.append(b"raw")
    with pytest.raises(LocalExecutionContractError) as caught:
        capture.finish()
    assert caught.value.code == "output_artifact_corrupt"
    capture.close()


def test_candidate_integration_preserves_approval_and_strict_cwd(tmp_path):
    from bdb_vnext.candidate_execution_bridge import CandidateExecutionBridge
    active, candidate = tmp_path / "active", tmp_path / "candidate"
    active.mkdir(); candidate.mkdir()
    req = classified_request(execution_id="candidate-script", project_id="p1", argv=(sys.executable, "-c", "print('approved fixture')"), expected_source_head="a"*40, expected_source_tree="b"*40)
    evaluator = ExecutionPolicyEvaluator()
    bridge = CandidateExecutionBridge(policy_evaluator=evaluator)
    args = dict(request=req, candidate_root=candidate, active_repo_root=active, current_head="a"*40, current_tree="b"*40, candidate_id="candidate")
    with pytest.raises(LocalExecutionContractError) as denied: bridge.execute_in_candidate(**args)
    assert denied.value.code == "policy_denied"
    token = evaluator.approval_registry.issue(req, effect_class=PolicyEffectClass.DESTRUCTIVE, validity_seconds=60)
    result, eligibility = bridge.execute_in_candidate(**args, approval_token=token)
    assert result.exit_code == 0 and eligibility is not None
    nested_active = active / "nested"
    nested_active.mkdir()
    escaped = replace(req, cwd=str(nested_active), request_digest="")
    token = evaluator.approval_registry.issue(escaped, effect_class=PolicyEffectClass.DESTRUCTIVE, validity_seconds=60)
    with pytest.raises(LocalExecutionContractError) as caught:
        bridge.execute_in_candidate(**{**args, "request": escaped}, approval_token=token)
    assert caught.value.code == "candidate_cwd_escape"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object fault boundary")
@pytest.mark.parametrize("fault", ["create", "assign"])
def test_windows_containment_fault_prevents_user_effect(tmp_path, monkeypatch, fault):
    marker = tmp_path / "must-not-exist"
    script = f"from pathlib import Path; Path({str(marker)!r}).write_text('effect')"
    req = classified_request(execution_id="job-fault", project_id="p1", argv=(sys.executable, "-c", script), expected_source_head="a"*40, expected_source_tree="b"*40)
    if fault == "create":
        class MissingJob:
            handle = None
        monkeypatch.setattr(runner_module, "WindowsJobObject", MissingJob)
    else:
        monkeypatch.setattr(runner_module.WindowsJobObject, "assign_process", lambda self, handle: False)
    with pytest.raises(LocalExecutionContractError) as caught:
        runner_module.StatelessWindowsProcessRunner().run(req, approved(req, tmp_path), "a"*40, "b"*40, tmp_path)
    assert caught.value.code == "process_containment_failed"
    assert not marker.exists()
    assert not list((tmp_path / "evidence").glob("capture-*.tmp"))
