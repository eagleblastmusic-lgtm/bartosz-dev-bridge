from __future__ import annotations

from typing import Any, Type

from .multi_file_patch_recovery_models import MultiFileCheckpointBundle


def install_multi_file_patch_checkpoint_hook_boundary(executor_cls: Type[object]) -> None:
    """Keep runtime crash hooks outside the static temp-ownership preflight."""

    if getattr(executor_cls, "_ghb2d_checkpoint_hook_boundary_installed", False):
        return
    original = executor_cls.checkpoint

    def checkpoint(self: Any, **kwargs: Any) -> MultiFileCheckpointBundle:
        fault_hook = kwargs.pop("fault_hook", None)
        bundle = original(self, **kwargs)
        if fault_hook is not None:
            fault_hook("AFTER_BATCH_CHECKPOINT")
        return bundle

    executor_cls.checkpoint = checkpoint
    setattr(executor_cls, "_ghb2d_checkpoint_hook_boundary_installed", True)
