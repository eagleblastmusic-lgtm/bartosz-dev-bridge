from __future__ import annotations

from typing import Any, Type



def install_multi_file_patch_lifecycle_bootstrap(runtime_cls: Type[object]) -> None:
    """Create the default preserve record once, without overwriting operator state."""

    if getattr(runtime_cls, "_ghb2d_lifecycle_bootstrap_installed", False):
        return
    original = runtime_cls._workspace

    def workspace_with_lifecycle(self: Any, session: Any, command_id: str):
        workspace = original(self, session, command_id)
        durable = self.journal.get_workspace(session.session_id)
        if durable is None:
            raise RuntimeError("workspace disappeared after ensure_workspace")
        lifecycle = self.journal.get_workspace_lifecycle(session.session_id)
        if lifecycle is None:
            self.journal.record_workspace_preserved(
                session_id=session.session_id,
                workspace_path=durable.workspace_path,
                base_sha=durable.base_sha,
                expected_revision=durable.revision,
                expected_state_hash=durable.state_hash,
            )
        return workspace

    runtime_cls._workspace = workspace_with_lifecycle
    setattr(runtime_cls, "_ghb2d_lifecycle_bootstrap_installed", True)
