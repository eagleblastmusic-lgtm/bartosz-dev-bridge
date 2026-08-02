from __future__ import annotations

from .git_status_untracked_hotfix import install_full_untracked_status
from .native_action_preflight import install_native_action_preflight
from .native_actions import NativeActionComposer
from .runtime_hardening import install_runtime_hardening
from .terminal_diagnostics import install_terminal_diagnostics
from .workspace_manager import Git


_INSTALLED = False


def install_native_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    install_full_untracked_status(Git)
    install_runtime_hardening()
    install_terminal_diagnostics()
    install_native_action_preflight(NativeActionComposer)
