from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import BridgeErrorCode, CommandState
from .protocol import BridgeError


def install_nested_create_hotfix(
    planner_type: type[Any],
    executor_type: type[Any],
    runtime_type: type[Any],
) -> None:
    """Install bounded support for creating files in new repository directories.

    The planner remains read-only. Missing parents are accepted only for
    ``create_file`` destinations. The executor creates each missing directory
    inside the already validated detached worktree and rechecks every new path
    for symlink/reparse escapes before writing the file.
    """

    if getattr(planner_type, "_bdb_nested_create_hotfix", False):
        return

    original_state = planner_type._state
    original_write_exact = executor_type._write_exact
    original_execute_or_recover = runtime_type.execute_or_recover

    def planner_state(
        self: Any,
        states: dict[str, Any],
        path_identities: dict[str, str],
        path: str,
        role: str,
        index: int,
        *,
        destination: bool = False,
    ) -> Any:
        try:
            return original_state(
                self,
                states,
                path_identities,
                path,
                role,
                index,
                destination=destination,
            )
        except BridgeError as exc:
            if not (
                destination
                and role == "create-destination"
                and exc.code == BridgeErrorCode.MISSING_FILE.value
            ):
                raise
            # Re-run all canonical path, scope, alias, and file-state checks;
            # skip only the legacy requirement that the parent already exists.
            return original_state(
                self,
                states,
                path_identities,
                path,
                role,
                index,
                destination=False,
            )

    def ensure_create_parent(self: Any, bundle: Any, target: Path) -> None:
        parent = target.parent
        if parent.is_dir():
            self.workspace._assert_no_reparse_escape(parent)
            return
        if parent.exists() or parent.is_symlink():
            self._block(bundle, f"Create parent is not a directory: {target.name}")

        root = self.workspace.path.resolve(strict=False)
        self.workspace._assert_no_reparse_escape(parent)
        try:
            parent.resolve(strict=False).relative_to(root)
        except ValueError:
            self._block(bundle, f"Create parent escaped workspace: {target.name}")

        missing: list[Path] = []
        cursor = parent
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        if not cursor.is_dir() or self.workspace._is_reparse(cursor):
            self._block(bundle, f"Create parent chain is unsafe: {target.name}")

        for directory in reversed(missing):
            try:
                directory.mkdir(exist_ok=True)
            except OSError as exc:
                self._block(
                    bundle,
                    f"Controlled create parent failure for {target.name}: {type(exc).__name__}",
                )
            self.workspace._assert_no_reparse_escape(directory)
            if not directory.is_dir() or self.workspace._is_reparse(directory):
                self._block(bundle, f"Created parent is unsafe: {target.name}")
            type(self.workspace)._fsync_parent(directory.parent)

    def write_exact(
        self: Any,
        bundle: Any,
        item: Any,
        *,
        expected: bytes | None,
        target: bytes,
        mode: str,
        fault_hook: Any,
    ) -> None:
        path = self.workspace.resolve_allowed_path(item.path)
        if expected is None and not path.parent.is_dir():
            ensure_create_parent(self, bundle, path)
        return original_write_exact(
            self,
            bundle,
            item,
            expected=expected,
            target=target,
            mode=mode,
            fault_hook=fault_hook,
        )

    def execute_or_recover(self: Any, command_id: str) -> Any:
        try:
            return original_execute_or_recover(self, command_id)
        except BridgeError as exc:
            if exc.code != BridgeErrorCode.MISSING_FILE.value:
                raise
            command = self.journal.get_command(command_id)
            if command is not None and command.state is CommandState.CLAIMED:
                self._terminal_claimed(command_id, CommandState.POLICY_DENIED)
                return None
            raise

    planner_type._state = planner_state
    executor_type._write_exact = write_exact
    runtime_type.execute_or_recover = execute_or_recover
    planner_type._bdb_nested_create_hotfix = True
