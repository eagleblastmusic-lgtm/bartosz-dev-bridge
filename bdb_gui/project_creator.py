from __future__ import annotations

import os
import re
import subprocess
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Protocol, Sequence

from bdb_bridge.native_host import default_native_config_path
from bdb_bridge.project_launch import ProjectLaunchQueue

from .operations import ProjectOperationsService
from .projects import PrepareResult, ProjectPrepareService, _normalize_allowed_paths
from .runtime_paths import default_python_executable


PROJECT_CREATOR_PLAN_SCHEMA = "bdb-project-creator-plan-v1"
PROJECT_CREATOR_RESULT_SCHEMA = "bdb-project-creator-result-v1"
ProjectCreatorMode = Literal["new", "existing"]
GitHubVisibility = Literal["private", "public"]
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_GITHUB_URL_RE = re.compile(
    r"^(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?|git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?)$"
)

DEFAULT_ALLOWED_PATHS = (
    "README.md",
    ".gitignore",
    "src/**",
    "tests/**",
    "app/**",
    "public/**",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "requirements*.txt",
    "*.sln",
    "*.csproj",
)


@dataclass(frozen=True)
class ProjectCommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProjectCommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 120.0,
    ) -> ProjectCommandResult:
        ...


class SubprocessProjectCommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 120.0,
    ) -> ProjectCommandResult:
        platform_options: dict[str, object] = {}
        if os.name == "nt":
            platform_options["creationflags"] = 0x08000000
        completed = subprocess.run(
            [str(item) for item in args],
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=timeout_seconds,
            **platform_options,
        )
        return ProjectCommandResult(
            args=tuple(str(item) for item in args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True)
class ProjectCreatorPlan:
    mode: ProjectCreatorMode
    alias: str
    project_name: str
    projects_root: str
    source_input: str
    source_repo: str
    github_visibility: GitHubVisibility
    prompt: str
    auto_send: bool
    allowed_paths: tuple[str, ...]
    python_executable: str
    test_timeout_seconds: int
    arm_minutes: int
    create_github_repository: bool
    requires_confirmation: bool = True
    read_only: bool = True
    mutation_operations_invoked: int = 0
    schema: str = PROJECT_CREATOR_PLAN_SCHEMA

    @property
    def workspace_root(self) -> str:
        return str(Path(self.projects_root).parent / "workspaces" / self.alias)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mode": self.mode,
            "alias": self.alias,
            "project_name": self.project_name,
            "projects_root": self.projects_root,
            "source_input": self.source_input,
            "source_repo": self.source_repo,
            "github_visibility": self.github_visibility,
            "prompt": self.prompt,
            "auto_send": self.auto_send,
            "allowed_paths": list(self.allowed_paths),
            "python_executable": self.python_executable,
            "test_timeout_seconds": self.test_timeout_seconds,
            "arm_minutes": self.arm_minutes,
            "create_github_repository": self.create_github_repository,
            "requires_confirmation": self.requires_confirmation,
            "read_only": self.read_only,
            "mutation_operations_invoked": self.mutation_operations_invoked,
        }


@dataclass(frozen=True)
class ProjectCreatorResult:
    plan: ProjectCreatorPlan
    ok: bool
    source_repo: str | None
    workspace_root: str | None
    github_url: str | None
    launch_id: str | None
    steps: tuple[str, ...] = field(default_factory=tuple)
    error_code: str | None = None
    error_message: str | None = None
    mutation_operations_invoked: int = 1
    schema: str = PROJECT_CREATOR_RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plan": self.plan.to_dict(),
            "ok": self.ok,
            "source_repo": self.source_repo,
            "workspace_root": self.workspace_root,
            "github_url": self.github_url,
            "launch_id": self.launch_id,
            "steps": list(self.steps),
            "mutation_operations_invoked": self.mutation_operations_invoked,
            "error": None if self.ok else {"code": self.error_code, "message": self.error_message},
        }


class ProjectCreatorService:
    """Create/import, prepare, start and hand a project prompt to ChatGPT.

    The service keeps one closed command catalog (git and gh only), never uses a
    shell, preserves all created files on failure, and delegates BDB preparation
    and lifecycle to the existing reviewed services.
    """

    def __init__(
        self,
        *,
        prepare_service: ProjectPrepareService | None = None,
        operations_service: ProjectOperationsService | None = None,
        command_runner: ProjectCommandRunner | None = None,
        launch_queue: ProjectLaunchQueue | None = None,
        browser_opener: Callable[[str], bool] | None = None,
    ) -> None:
        self._prepare_service = prepare_service or ProjectPrepareService()
        self._operations_service = operations_service or ProjectOperationsService()
        self._runner = command_runner or SubprocessProjectCommandRunner()
        queue_path = default_native_config_path().parent / "project-launch-queue.json"
        self._launch_queue = launch_queue or ProjectLaunchQueue(queue_path)
        self._browser_opener = browser_opener or webbrowser.open

    def build_plan(
        self,
        *,
        workspaces_root: str | Path,
        mode: str,
        alias: str,
        project_name: str,
        projects_root: str | Path,
        source_input: str = "",
        github_visibility: str = "private",
        prompt: str,
        auto_send: bool = True,
        allowed_paths: Iterable[str] = DEFAULT_ALLOWED_PATHS,
        python_executable: str | Path | None = None,
        test_timeout_seconds: int = 180,
        arm_minutes: int = 30,
    ) -> ProjectCreatorPlan:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"new", "existing"}:
            raise ValueError("mode must be new or existing")
        normalized_alias = alias.strip().lower()
        if _ALIAS_RE.fullmatch(normalized_alias) is None:
            raise ValueError("alias must match ^[a-z][a-z0-9-]{0,31}$")
        normalized_name = project_name.strip()
        if _REPO_NAME_RE.fullmatch(normalized_name) is None:
            raise ValueError("project_name has an unsafe GitHub repository format")
        visibility = github_visibility.strip().lower()
        if visibility not in {"private", "public"}:
            raise ValueError("github_visibility must be private or public")
        normalized_prompt = prompt.strip()
        if not normalized_prompt or len(normalized_prompt) > 40_000:
            raise ValueError("prompt must be non-empty and at most 40000 characters")
        if not isinstance(auto_send, bool):
            raise ValueError("auto_send must be boolean")
        if isinstance(test_timeout_seconds, bool) or not 1 <= int(test_timeout_seconds) <= 3_600:
            raise ValueError("test_timeout_seconds must be between 1 and 3600")
        if isinstance(arm_minutes, bool) or not 1 <= int(arm_minutes) <= 60:
            raise ValueError("arm_minutes must be between 1 and 60")

        workspace_parent = Path(workspaces_root).expanduser().resolve(strict=False)
        if not workspace_parent.is_dir():
            raise ValueError("workspaces_root must be an existing directory")
        if (workspace_parent / normalized_alias).exists():
            raise ValueError("workspace alias already exists")

        parent = Path(projects_root).expanduser().resolve(strict=False)
        if not parent.is_dir():
            raise ValueError("projects_root must be an existing directory")
        source_text = source_input.strip()
        if normalized_mode == "new":
            source = (parent / normalized_name).resolve(strict=False)
            if source.exists():
                raise ValueError("new project source path already exists")
            create_github = True
        else:
            candidate = Path(source_text).expanduser().resolve(strict=False)
            if candidate.is_dir() and candidate.joinpath(".git").exists():
                source = candidate
            elif _GITHUB_URL_RE.fullmatch(source_text):
                source = (parent / normalized_name).resolve(strict=False)
                if source.exists():
                    raise ValueError("clone destination already exists")
            else:
                raise ValueError("existing project requires a local Git checkout or a GitHub clone URL")
            create_github = False

        python_path = Path(python_executable or default_python_executable()).expanduser().resolve(strict=False)
        if not python_path.is_file():
            raise ValueError("python_executable must be an existing file")

        return ProjectCreatorPlan(
            mode=normalized_mode,  # type: ignore[arg-type]
            alias=normalized_alias,
            project_name=normalized_name,
            projects_root=str(parent),
            source_input=source_text,
            source_repo=str(source),
            github_visibility=visibility,  # type: ignore[arg-type]
            prompt=normalized_prompt,
            auto_send=auto_send,
            allowed_paths=_normalize_allowed_paths(allowed_paths),
            python_executable=str(python_path),
            test_timeout_seconds=int(test_timeout_seconds),
            arm_minutes=int(arm_minutes),
            create_github_repository=create_github,
        )

    def execute(self, plan: ProjectCreatorPlan, *, workspaces_root: str | Path) -> ProjectCreatorResult:
        if not isinstance(plan, ProjectCreatorPlan):
            raise TypeError("project creator requires a validated ProjectCreatorPlan")
        steps: list[str] = []
        github_url: str | None = None
        source = Path(plan.source_repo)
        workspace_root: str | None = None
        try:
            self._require_command(("git", "--version"), "git_unavailable")
            if plan.mode == "new":
                self._require_command(("gh", "--version"), "github_cli_unavailable")
                self._require_command(("gh", "auth", "status"), "github_auth_required")
                self._initialize_new_repository(source, plan)
                steps.append("local_repository_created")
                github_url = self._create_github_repository(source, plan)
                steps.append("github_repository_created_and_pushed")
            elif not source.is_dir():
                self._run_checked(("git", "clone", "--", plan.source_input, str(source)), timeout_seconds=300.0)
                steps.append("existing_github_repository_cloned")
            else:
                steps.append("existing_local_repository_selected")

            self._ensure_clean_attached_repository(source)
            prepare_plan = self._prepare_service.build_plan(
                workspaces_root=workspaces_root,
                alias=plan.alias,
                source_repo=source,
                allowed_paths=plan.allowed_paths,
                python_executable=plan.python_executable,
                test_timeout_seconds=plan.test_timeout_seconds,
            )
            prepare_result: PrepareResult = self._prepare_service.execute(prepare_plan)
            if not prepare_result.ok:
                raise RuntimeError(
                    f"prepare_failed:{prepare_result.error_code or 'unknown'}:{prepare_result.error_message or ''}"
                )
            workspace_root = prepare_plan.workspace_root
            steps.append("bdb_workspace_prepared")

            start_result = self._operations_service.execute(
                "start",
                workspace_root,
                arm_minutes=plan.arm_minutes,
            )
            if not start_result.ok:
                raise RuntimeError(
                    f"start_failed:{start_result.error_code or 'unknown'}:{start_result.error_message or ''}"
                )
            steps.append("bridge_started_and_armed")

            launch = self._launch_queue.enqueue(
                repo_alias=plan.alias,
                prompt=self._launch_prompt(plan),
                auto_send=plan.auto_send,
                ttl_minutes=min(plan.arm_minutes, 30),
            )
            steps.append("chatgpt_prompt_queued")
            self._browser_opener("https://chatgpt.com/")
            steps.append("chatgpt_opened")
            return ProjectCreatorResult(
                plan=plan,
                ok=True,
                source_repo=str(source),
                workspace_root=workspace_root,
                github_url=github_url or self._origin_url(source),
                launch_id=launch.launch_id,
                steps=tuple(steps),
            )
        except Exception as error:
            return ProjectCreatorResult(
                plan=plan,
                ok=False,
                source_repo=str(source) if source.exists() else None,
                workspace_root=workspace_root,
                github_url=github_url,
                launch_id=None,
                steps=tuple(steps),
                error_code=self._error_code(error),
                error_message=str(error),
            )

    def _initialize_new_repository(self, source: Path, plan: ProjectCreatorPlan) -> None:
        source.mkdir(parents=False, exist_ok=False)
        source.joinpath("README.md").write_text(
            f"# {plan.project_name}\n\nCreated by BDB Project Creator.\n",
            encoding="utf-8",
            newline="\n",
        )
        source.joinpath(".gitignore").write_text(
            ".venv/\nnode_modules/\nbin/\nobj/\n.env\n",
            encoding="utf-8",
            newline="\n",
        )
        self._run_checked(("git", "init", "--initial-branch=main"), cwd=source)
        self._run_checked(("git", "config", "user.name", "Bartosz Dev Bridge"), cwd=source)
        self._run_checked(("git", "config", "user.email", "bdb@localhost.invalid"), cwd=source)
        self._run_checked(("git", "add", "--", "README.md", ".gitignore"), cwd=source)
        self._run_checked(("git", "commit", "-m", "chore: initialize project"), cwd=source)

    def _create_github_repository(self, source: Path, plan: ProjectCreatorPlan) -> str | None:
        visibility_flag = "--private" if plan.github_visibility == "private" else "--public"
        completed = self._run_checked(
            (
                "gh",
                "repo",
                "create",
                plan.project_name,
                visibility_flag,
                "--source",
                str(source),
                "--remote",
                "origin",
                "--push",
            ),
            cwd=source,
            timeout_seconds=300.0,
        )
        for line in reversed(completed.stdout.splitlines()):
            candidate = line.strip()
            if candidate.startswith("https://github.com/"):
                return candidate
        return self._origin_url(source)

    def _ensure_clean_attached_repository(self, source: Path) -> None:
        if not source.joinpath(".git").exists():
            raise RuntimeError("source repository is not a non-bare Git checkout")
        if self._run_checked(("git", "status", "--porcelain=v1"), cwd=source).stdout.strip():
            raise RuntimeError("source repository must be clean")
        branch = self._run_checked(("git", "symbolic-ref", "-q", "--short", "HEAD"), cwd=source).stdout.strip()
        if not branch:
            raise RuntimeError("source repository must be attached to a branch")

    def _origin_url(self, source: Path) -> str | None:
        completed = self._runner.run(("git", "remote", "get-url", "origin"), cwd=source, timeout_seconds=30.0)
        value = completed.stdout.strip()
        return value if completed.returncode == 0 and value else None

    def _require_command(self, args: Sequence[str], code: str) -> None:
        completed = self._runner.run(args, timeout_seconds=30.0)
        if completed.returncode != 0:
            raise RuntimeError(code)

    def _run_checked(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 120.0,
    ) -> ProjectCommandResult:
        completed = self._runner.run(args, cwd=cwd, timeout_seconds=timeout_seconds)
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout)[-2_000:].strip()
            raise RuntimeError(f"command_failed:{' '.join(completed.args)}:{tail}")
        return completed

    @staticmethod
    def _error_code(error: Exception) -> str:
        text = str(error)
        if text in {"git_unavailable", "github_cli_unavailable", "github_auth_required"}:
            return text
        if text.startswith("prepare_failed:"):
            return "prepare_failed"
        if text.startswith("start_failed:"):
            return "start_failed"
        if text.startswith("command_failed:"):
            return "project_command_failed"
        return "project_creator_failed"

    @staticmethod
    def _launch_prompt(plan: ProjectCreatorPlan) -> str:
        return (
            "Pracujemy przez Bartosz Dev Bridge w trybie AUTO.\n\n"
            f"Repo alias: {plan.alias}\n"
            f"Projekt: {plan.project_name}\n"
            f"Zadanie użytkownika: {plan.prompt}\n\n"
            "Wykonaj cały bezpieczny przebieg w tej rozmowie: workspace_context → analiza → edycja "
            "wyłącznie dozwolonych plików → test profilu odpowiedniego dla wykrytego stosu → analiza błędu "
            "i poprawka, gdy potrzebna → ponowny test → lokalna promocja zielonego wyniku → końcowe "
            "workspace_context potwierdzające source_clean i receipt promocji. Nie wykonuj pushu, merge, "
            "deployu ani zmian poza allowlistą. Zacznij od workspace_context dla wskazanego aliasu."
        )
