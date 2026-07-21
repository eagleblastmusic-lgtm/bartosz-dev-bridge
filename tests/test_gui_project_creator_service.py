from __future__ import annotations

import sys
from pathlib import Path

from bdb_bridge.project_launch import ProjectLaunchQueue
from bdb_operator.models import OperatorResponse
from bdb_operator.project_creator import ProjectCommandResult, ProjectCreatorService


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run(self, args, *, cwd=None, timeout_seconds=120.0):
        command = tuple(str(item) for item in args)
        root = str(cwd) if cwd is not None else None
        self.calls.append((command, root))
        stdout = ""
        if command[:2] == ("git", "init") and cwd is not None:
            Path(cwd, ".git").mkdir()
        elif command[:4] == ("git", "symbolic-ref", "-q", "--short"):
            stdout = "main\n"
        elif command[:3] == ("git", "remote", "get-url"):
            stdout = "https://github.com/example/calculator.git\n"
        elif command[:3] == ("gh", "repo", "create"):
            stdout = "https://github.com/example/calculator\n"
        return ProjectCommandResult(command, 0, stdout, "")


class FakeProjectCreatorOperator:
    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, object]] = []
        self.starts: list[tuple[str, int]] = []

    def prepare(
        self,
        workspace_root,
        *,
        source_repo,
        alias,
        allowed_paths,
        test_timeout_seconds=120.0,
        python_executable=None,
    ):
        self.prepare_calls.append(
            {
                "workspace_root": str(workspace_root),
                "source_repo": str(source_repo),
                "alias": alias,
                "allowed_paths": tuple(allowed_paths),
            }
        )
        Path(workspace_root).mkdir()
        return OperatorResponse.success(
            "prepare",
            project_alias=alias,
            data={"status": "prepared", "workspace_root": str(workspace_root)},
        )

    def start(self, workspace_root, *, arm_minutes=30):
        self.starts.append((str(workspace_root), arm_minutes))
        return OperatorResponse.success(
            "start",
            project_alias="calculator",
            data={"status": "RUNNING", "arm": {"armed": True}},
        )


def make_service(tmp_path: Path):
    runner = FakeRunner()
    operator = FakeProjectCreatorOperator()
    queue = ProjectLaunchQueue(tmp_path / "project-launch-queue.json")
    opened: list[str] = []
    service = ProjectCreatorService(
        operator=operator,
        command_runner=runner,
        launch_queue=queue,
        browser_opener=lambda url: opened.append(url) or True,
    )
    return service, runner, operator, queue, opened


def test_new_project_creates_github_prepares_starts_and_queues_prompt(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    projects = tmp_path / "projects"
    workspaces.mkdir()
    projects.mkdir()
    service, runner, operator, queue, opened = make_service(tmp_path)

    plan = service.build_plan(
        workspaces_root=workspaces,
        mode="new",
        alias="calculator",
        project_name="calculator",
        projects_root=projects,
        prompt="Create a calculator",
        python_executable=sys.executable,
    )
    result = service.execute(plan)

    assert result.ok is True
    assert result.github_url == "https://github.com/example/calculator"
    assert Path(result.source_repo, "README.md").is_file()
    assert operator.prepare_calls[0]["alias"] == "calculator"
    assert operator.prepare_calls[0]["allowed_paths"][-3:] == ("*.cmd", "*.bat", "*.ps1")
    assert operator.starts == [(str(workspaces / "calculator"), 30)]
    launch = queue.peek()
    assert launch is not None
    assert launch.repo_alias == "calculator"
    assert "Create a calculator" in launch.prompt
    assert "Python 3 + pytest" in launch.prompt
    assert "Efektywna allowlista" in launch.prompt
    assert "- src/**" in launch.prompt
    assert "- *.cmd" in launch.prompt
    assert launch.auto_send is True
    assert opened == []
    assert "chatgpt_active_conversation_waiting" in result.steps
    assert "chatgpt_opened" not in result.steps
    commands = [call[0] for call in runner.calls]
    assert ("gh", "auth", "status") in commands
    assert any(command[:3] == ("gh", "repo", "create") for command in commands)


def test_explicit_allowlist_is_not_silently_widened(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    projects = tmp_path / "projects"
    workspaces.mkdir()
    projects.mkdir()
    service, *_ = make_service(tmp_path)

    plan = service.build_plan(
        workspaces_root=workspaces,
        mode="new",
        alias="strict",
        project_name="strict",
        projects_root=projects,
        prompt="Create strict project",
        allowed_paths=("src/**",),
        python_executable=sys.executable,
    )

    assert plan.allowed_paths == ("src/**",)


def test_existing_local_project_skips_github_creation(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    projects = tmp_path / "projects"
    source = projects / "existing"
    workspaces.mkdir()
    source.mkdir(parents=True)
    (source / ".git").mkdir()
    service, runner, operator, queue, _opened = make_service(tmp_path)

    plan = service.build_plan(
        workspaces_root=workspaces,
        mode="existing",
        alias="existing",
        project_name="existing",
        projects_root=projects,
        source_input=str(source),
        prompt="Add tests",
        python_executable=sys.executable,
        auto_send=False,
    )
    result = service.execute(plan)

    assert result.ok is True
    assert operator.starts == [(str(workspaces / "existing"), 30)]
    assert queue.peek() is not None
    assert queue.peek().auto_send is False
    assert not any(call[0][0] == "gh" for call in runner.calls)


def test_plan_rejects_existing_workspace_alias(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    projects = tmp_path / "projects"
    workspaces.mkdir()
    projects.mkdir()
    (workspaces / "calculator").mkdir()
    service, *_ = make_service(tmp_path)

    try:
        service.build_plan(
            workspaces_root=workspaces,
            mode="new",
            alias="calculator",
            project_name="calculator",
            projects_root=projects,
            prompt="Create a calculator",
            python_executable=sys.executable,
        )
    except ValueError as error:
        assert "workspace alias already exists" in str(error)
    else:  # pragma: no cover
        raise AssertionError("duplicate alias was accepted")
