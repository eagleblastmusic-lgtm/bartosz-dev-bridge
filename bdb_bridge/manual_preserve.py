from __future__ import annotations

from typing import Type


def _install_pre_preserve(journal_cls: Type[object], name: str, *, command_arg: bool) -> None:
    original = getattr(journal_cls, name, None)
    if original is None or getattr(original, "_ghb07_pre_preserve", False):
        return

    def wrapped(self: object, *args: object, **kwargs: object):
        if command_arg:
            command_id = str(kwargs.get("command_id") or (args[0] if args else ""))
            command = self.get_command(command_id) if command_id else None
            session_id = command.session_id if command is not None else ""
        else:
            session_id = str(kwargs.get("session_id") or (args[0] if args else ""))
        workspace = self.get_workspace(session_id) if session_id else None
        if workspace is not None and self.get_workspace_lifecycle(session_id) is None:
            self.record_workspace_preserved(
                session_id=session_id,
                workspace_path=workspace.workspace_path,
                base_sha=workspace.base_sha,
                expected_revision=workspace.revision,
                expected_state_hash=workspace.state_hash,
            )
        return original(self, *args, **kwargs)

    wrapped._ghb07_pre_preserve = True
    setattr(journal_cls, name, wrapped)


def install_manual_pre_preserve(journal_cls: Type[object]) -> None:
    _install_pre_preserve(journal_cls, "mark_workspace_recovery_blocked", command_arg=False)
    _install_pre_preserve(journal_cls, "mark_result_collision", command_arg=True)
