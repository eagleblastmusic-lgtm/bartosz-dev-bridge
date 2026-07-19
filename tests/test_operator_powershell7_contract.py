from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from bdb_operator.cli import _parser
from bdb_operator.operator import OperatorApi
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


class MissingExecutableRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> CompletedCommand:
        frozen = tuple(str(item) for item in args)
        self.calls.append((frozen, timeout_seconds))
        raise FileNotFoundError(frozen[0])


def completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> CompletedCommand:
    return CompletedCommand(args=(), returncode=returncode, stdout=stdout, stderr=stderr)


def repo_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "bridge"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "Invoke-BDBWorkspaceLoop.ps1").write_text("# fixture\n", encoding="utf-8")
    return repo


def workspace_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces" / "calculator2"
    root.mkdir(parents=True)
    state = {
        "schema": "bdb-workspace-loop-state-v1",
        "status": "prepared",
        "alias": "calculator2",
        "source_repo": str(tmp_path / "source-calculator2"),
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
    (root / "workspace-loop-state.json").write_text(json.dumps(state), encoding="utf-8")
    return root


def status_document() -> str:
    return json.dumps({"status": "NOT_READY", "alias": "calculator2"})


def test_public_operator_defaults_to_pwsh7_and_probes_only_once(tmp_path: Path) -> None:
    runner = FakeRunner(
        completed("7\n"),
        completed(status_document()),
        completed(status_document()),
    )
    api = OperatorApi(repo_root=repo_fixture(tmp_path), runner=runner, platform_name="nt")
    workspace = workspace_fixture(tmp_path)

    first = api.status(workspace)
    second = api.status(workspace)
    capabilities = api.capabilities()

    assert first.ok is True and second.ok is True
    assert len(runner.calls) == 3
    probe_args, probe_timeout = runner.calls[0]
    assert probe_args == (
        "pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$PSVersionTable.PSVersion.Major",
    )
    assert probe_timeout == 10.0
    assert runner.calls[1][0][0] == "pwsh.exe"
    assert runner.calls[2][0][0] == "pwsh.exe"
    assert capabilities.data["powershell"] == {
        "executable": "pwsh.exe",
        "required_major": 7,
        "validated_major": 7,
        "fallback_to_windows_powershell": False,
    }


def test_custom_pwsh7_path_is_preserved(tmp_path: Path) -> None:
    executable = r"C:\Program Files\PowerShell\7\pwsh.exe"
    runner = FakeRunner(completed("7\n"), completed(status_document()))
    api = OperatorApi(
        repo_root=repo_fixture(tmp_path),
        runner=runner,
        platform_name="nt",
        powershell_executable=executable,
    )

    response = api.status(workspace_fixture(tmp_path))

    assert response.ok is True
    assert runner.calls[0][0][0] == executable
    assert runner.calls[1][0][0] == executable


def test_windows_powershell_51_is_rejected_without_fallback(tmp_path: Path) -> None:
    runner = FakeRunner(completed("5\n"))
    api = OperatorApi(
        repo_root=repo_fixture(tmp_path),
        runner=runner,
        platform_name="nt",
        powershell_executable="powershell.exe",
    )

    response = api.status(workspace_fixture(tmp_path))

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "powershell_version_unsupported"
    assert response.error.details == {
        "executable": "powershell.exe",
        "required_major": 7,
        "detected_major": 5,
        "fallback_to_windows_powershell": False,
    }
    assert len(runner.calls) == 1


def test_missing_pwsh_returns_explicit_executable_error(tmp_path: Path) -> None:
    runner = MissingExecutableRunner()
    api = OperatorApi(repo_root=repo_fixture(tmp_path), runner=runner, platform_name="nt")

    response = api.status(workspace_fixture(tmp_path))

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "executable_missing"
    assert response.error.details == {"executable": "pwsh.exe"}
    assert len(runner.calls) == 1


def test_cli_does_not_force_windows_powershell_51() -> None:
    args = _parser().parse_args(["capabilities"])

    assert args.powershell is None
