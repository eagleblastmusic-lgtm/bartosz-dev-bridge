from __future__ import annotations

from typing import Any


def install_full_untracked_status(git_type: type[Any]) -> None:
    """Make porcelain status enumerate untracked files instead of directory summaries."""

    if getattr(git_type, "_bdb_full_untracked_status", False):
        return

    original_run = git_type.run

    def run(
        self: Any,
        args: Any,
        *,
        cwd: Any = None,
        check: bool = True,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> Any:
        normalized = list(args)
        if (
            normalized[:2] == ["status", "--porcelain=v1"]
            and not any(value.startswith("--untracked-files") for value in normalized)
        ):
            normalized.append("--untracked-files=all")
        return original_run(
            self,
            normalized,
            cwd=cwd,
            check=check,
            timeout=timeout,
            env=env,
        )

    git_type.run = run
    git_type._bdb_full_untracked_status = True
