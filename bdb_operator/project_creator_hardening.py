from __future__ import annotations

from dataclasses import replace
from typing import Any


_INSTALLED = False
_SAFE_ROOT_LAUNCHERS = ("*.cmd", "*.bat", "*.ps1")


def install_project_creator_hardening(project_creator_service: type) -> None:
    """Keep project handoff in the active chat and expose the exact path contract."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_init = project_creator_service.__init__
    original_build_plan = project_creator_service.build_plan
    original_execute = project_creator_service.execute
    original_launch_prompt = project_creator_service._launch_prompt

    def hardened_init(self: Any, *args: Any, **kwargs: Any) -> None:
        # A queued launch is claimed by the visible focused ChatGPT conversation.
        # The desktop service must never create a competing tab or window.
        kwargs["browser_opener"] = lambda _url: True
        original_init(self, *args, **kwargs)

    def hardened_build_plan(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Root launchers are common bounded project entry points. Include them in
        # new Creator defaults so generated launchers cannot contradict the exact
        # Bridge/Promoter allowlist. Explicit user allowlists remain untouched.
        if "allowed_paths" not in kwargs:
            defaults = tuple(original_build_plan.__defaults__ or ())
            _ = defaults  # keep introspection stable; defaults are keyword-only.
            from .project_creator import DEFAULT_ALLOWED_PATHS

            kwargs["allowed_paths"] = (*DEFAULT_ALLOWED_PATHS, *_SAFE_ROOT_LAUNCHERS)
        return original_build_plan(self, *args, **kwargs)

    def hardened_execute(self: Any, plan: Any) -> Any:
        result = original_execute(self, plan)
        if not getattr(result, "ok", False):
            return result
        steps = tuple(
            "chatgpt_active_conversation_waiting"
            if step == "chatgpt_opened"
            else step
            for step in result.steps
        )
        return replace(result, steps=steps)

    @staticmethod
    def hardened_launch_prompt(plan: Any) -> str:
        prompt = original_launch_prompt(plan)
        allowed = "\n".join(f"- {pattern}" for pattern in plan.allowed_paths)
        return (
            f"{prompt}\n\n"
            "Efektywna allowlista tego workspace jest jedynym kontraktem ścieżek dla "
            "Creator → Native Host → Bridge → Promoter:\n"
            f"{allowed}\n"
            "Nie generuj operacji dla ścieżek spoza tej listy. Gdy zadanie naprawdę wymaga "
            "innej ścieżki, zakończ przed mutacją z dokładną propozycją rozszerzenia allowlisty."
        )

    project_creator_service.__init__ = hardened_init
    project_creator_service.build_plan = hardened_build_plan
    project_creator_service.execute = hardened_execute
    project_creator_service._launch_prompt = hardened_launch_prompt
