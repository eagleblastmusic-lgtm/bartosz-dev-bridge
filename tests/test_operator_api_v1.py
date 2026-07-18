from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from bdb_operator.api import OperatorApi
from bdb_operator.cli import main as operator_main
from bdb_operator.models import OPERATOR_PROJECT_SCHEMA, OPERATOR_RESPONSE_SCHEMA
from bdb_operator.runner import CompletedCommand


class FakeRunner:
    def __init__(self, *results: CompletedCommand) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CompletedCommand:
        frozen = tuple(str(item) for item in args)
        self.calls.append((frozen, timeout_seconds))
        if not self.results:
            raise AssertionError(f"Unexpected command: {frozen}")
        result = self.results.pop(0)
        return CompletedCommand(
            args=frozen,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def completed(value: dict[str, object], *, returncode: int = 0, stderr: str = "") -> CompletedCommand:
    return CompletedCommand(
        args=(),
        returncode=returncode,
        stdout=json.dumps(value),
        stderr=stderr,
    )


def repo_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "bridge"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "Invoke-BDBWorkspaceLoop.ps1").write_text("# fixture\n", encoding="utf-8")
    (scripts / "prepare_workspace_loop.py").write_text("# fixture\n", encoding="utf-8")
    return repo


def workspace_fixture(tmp_path: Path, *, name: str = "calculator2", schema: str = "bdb-workspace-loop-state-v1") -> Path:
    root = tmp_path / "workspaces" / name
    root.mkdir(parents=True)
    state = {
        "schema": schema,
        "status": "prepared",
        "alias": name,
        "source_repo": str(tmp_path / f"source-{name}"),
        "source_branch": "main",
        "source_head": "a" * 40,
        "root": str(root),
        "bridge_config": str(root / "bridge-config.json"),
        "native_config": str(tmp_path / "native-host.json"),
        "python_executable": str(tmp_path / "python.exe"),
        "promoter_script": str(tmp_path / "promoter.py"),
        "promoter_pid_file": str(root / "promoter.pid"),
        "promoter_stop_file": str(root / "promoter.stop"),
        "promoter_stdout": str(root / "promoter.out.log"),
        "promoter_stderr": str(root / "promoter.err.log"),
        "allowed_paths": ["*.py", "tests/*.py"],
    }
    (root / "workspace-loop-state.json").write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    return root


def test_capabilities_are_read_only_and_network_free(tmp_path: Path) -> None:
    runner = FakeRunner()
    api = OperatorApi(repo_root=repo_fixture(tmp_path), runner=runner, platform_name="nt")

    response = api.capabilities()

    assert response.ok is True
    assert response.schema == OPERATOR_RESPONSE_SCHEMA
    assert response.data["transport"] == "in_process"
    assert response.data["network_listener"] is False
    assert response.data["arbitrary_shell"] is False
    assert runner.calls == []


def test_status_uses_exact_existing_operator_without_hidden_start(tmp_path: Path) -> None:
    repo = repo_fixture(tmp_path)
    workspace = workspace_fixture(tmp_path)
    runner = FakeRunner(completed({"status": "READY", "alias": "calculator2"}))
    api = OperatorApi(
        repo_root=repo,
        runner=runner,
        platform_name="nt",
        powershell_executable="powershell-fixture.exe",
    )

    response = api.status(workspace)

    assert response.ok is True
    assert response.project_alias == "calculator2"
    assert response.data["status"] == "READY"
    assert len(runner.calls) == 1
    args, timeout = runner.calls[0]
    assert args[0] == "powershell-fixture.exe"
    assert args[args.index("-Action") + 1] == "Status"
    assert args[args.index("-Root") + 1] == str(workspace.resolve())
    assert "Start" not in args
    assert "native-host" not in args
    assert timeout == 60.0


def test_start_stop_and_rearm_have_separate_explicit_commands(tmp_path: Path) -> None:
    repo = repo_fixture(tmp_path)
    workspace = workspace_fixture(tmp_path)
    runner = FakeRunner(
        completed({"status": "RUNNING"}),
        completed({"status": "OFFLINE"}),
        completed({"armed": True, "armed_until": "2026-07-18T20:00:00Z"}),
    )
    api = OperatorApi(
        repo_root=repo,
        runner=runner,
        platform_name="nt",
        powershell_executable="powershell-fixture.exe",
    )

    started = api.start(workspace, arm_minutes=17)
    stopped = api.stop(workspace)
    rearmed = api.rearm(workspace, arm_minutes=9)

    assert started.ok and stopped.ok and rearmed.ok
    start_args = runner.calls[0][0]
    stop_args = runner.calls[1][0]
    rearm_args = runner.calls[2][0]
    assert start_args[start_args.index("-Action") + 1] == "Start"
    assert start_args[start_args.index("-ArmMinutes") + 1] == "17"
    assert stop_args[stop_args.index("-Action") + 1] == "Stop"
    assert "-ArmMinutes" not in stop_args
    assert rearm_args[1:6] == ("-m", "bdb_bridge", "bridge", "native-host", "arm")
    assert rearm_args[rearm_args.index("--minutes") + 1] == "9"


def test_invalid_arm_minutes_never_execute_a_command(tmp_path: Path) -> None:
    runner = FakeRunner()
    api = OperatorApi(repo_root=repo_fixture(tmp_path), runner=runner, platform_name="nt")

    response = api.start(workspace_fixture(tmp_path), arm_minutes=0)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "invalid_argument"
    assert runner.calls == []


def test_list_projects_is_read_only_and_reports_invalid_entries(tmp_path: Path) -> None:
    repo = repo_fixture(tmp_path)
    workspaces = tmp_path / "workspaces"
    workspace_fixture(tmp_path, name="alpha")
    workspace_fixture(tmp_path, name="broken", schema="unsupported")
    runner = FakeRunner()
    api = OperatorApi(repo_root=repo, runner=runner, platform_name="nt")

    response = api.list_projects(workspaces)

    assert response.ok is True
    assert runner.calls == []
    assert [project["alias"] for project in response.data["projects"]] == ["alpha"]
    assert response.data["projects"][0]["schema"] == OPERATOR_PROJECT_SCHEMA
    assert response.data["invalid_entries"][0]["code"] == "workspace_state_invalid"


def test_missing_state_and_invalid_command_json_use_stable_errors(tmp_path: Path) -> None:
    repo = repo_fixture(tmp_path)
    runner = FakeRunner(
        CompletedCommand(args=(), returncode=0, stdout="not-json", stderr=""),
    )
    api = OperatorApi(repo_root=repo, runner=runner, platform_name="nt")

    missing = api.status(tmp_path / "missing")
    invalid = api.status(workspace_fixture(tmp_path))

    assert missing.ok is False
    assert missing.error is not None
    assert missing.error.code == "workspace_state_missing"
    assert invalid.ok is False
    assert invalid.error is not None
    assert invalid.error.code == "invalid_response"


def test_prepare_uses_existing_preparer_and_repeated_allowlist_arguments(tmp_path: Path) -> None:
    repo = repo_fixture(tmp_path)
    runner = FakeRunner(completed({"alias": "alpha", "status": "prepared"}))
    api = OperatorApi(repo_root=repo, runner=runner, platform_name="nt")

    response = api.prepare(
        tmp_path / "workspaces" / "alpha",
        source_repo=tmp_path / "source",
        alias="alpha",
        allowed_paths=["*.py", "tests/*.py"],
        test_timeout_seconds=45,
        python_executable=tmp_path / "python.exe",
    )

    assert response.ok is True
    args, timeout = runner.calls[0]
    assert args[0] == str((tmp_path / "python.exe").resolve())
    assert args[1] == str((repo / "scripts" / "prepare_workspace_loop.py").resolve())
    assert args.count("--allowed-path") == 2
    assert args[args.index("--alias") + 1] == "alpha"
    assert timeout == 75.0


def test_non_windows_mutation_fails_without_execution(tmp_path: Path) -> None:
    runner = FakeRunner()
    api = OperatorApi(repo_root=repo_fixture(tmp_path), runner=runner, platform_name="posix")

    response = api.stop(workspace_fixture(tmp_path))

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "unsupported_platform"
    assert runner.calls == []


def test_capabilities_cli_prints_versioned_json(capsys) -> None:
    exit_code = operator_main(["capabilities"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["schema"] == OPERATOR_RESPONSE_SCHEMA
    assert output["operation"] == "capabilities"
    assert output["ok"] is True
