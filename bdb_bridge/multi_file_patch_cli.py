from __future__ import annotations

import json
import sys
from typing import Any

from . import cli as _legacy
from . import ghb07_cli as _base
from .command_timing import build_command_timing
from .journal import Journal
from .models import BridgeErrorCode
from .multi_file_patch_gate import MULTI_FILE_PATCH_OPERATION
from .protocol import BridgeError


_ORIGINAL_MAIN = _base.main
_ORIGINAL_PARSER = _base._parser


def _operation(command_json: str) -> str | None:
    try:
        value = json.loads(command_json)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return value.get("operation") if isinstance(value, dict) else None


def _parser():
    parser = _ORIGINAL_PARSER()
    bridge = next(
        action
        for action in parser._actions
        if getattr(action, "dest", None) == "command"
    ).choices["bridge"]
    commands = next(
        action
        for action in bridge._actions
        if getattr(action, "dest", None) == "bridge_command"
    )
    edit = commands.add_parser("edit")
    edit_commands = edit.add_subparsers(dest="edit_command", required=True)
    status = edit_commands.add_parser("status")
    status.add_argument("--config", required=True)
    status.add_argument("--command-id", required=True)
    status.add_argument("--json", action="store_true")
    return parser


def _edit_status(config: Any, command_id: str, output_json: bool) -> int:
    journal = None
    try:
        journal = Journal.open(config.journal_path)
        command = journal.get_command(command_id)
        if command is None:
            raise BridgeError(BridgeErrorCode.JOURNAL_CONFLICT, "Command not found")
        if _operation(command.command_json) != MULTI_FILE_PATCH_OPERATION:
            raise BridgeError(
                BridgeErrorCode.UNSUPPORTED_OPERATION,
                "Command is not a multi_file_patch operation",
            )
        checkpoint = journal.get_multi_file_patch_checkpoint(command_id)
        profile = journal.get_multi_file_patch_profile_run(command_id)
        result = journal.get_result(command_id)
        outbox = journal.get_outbox(command_id)
        timing = build_command_timing(journal, command_id)
        payload: dict[str, object] = {
            "command_id": command.command_id,
            "session_id": command.session_id,
            "sequence": command.sequence,
            "command_state": command.state.value,
            "checkpoint_state": checkpoint.state.value if checkpoint is not None else None,
            "checkpoint_sha256": checkpoint.checkpoint_sha256 if checkpoint is not None else None,
            "workspace_revision_before": (
                checkpoint.workspace_revision_before if checkpoint is not None else None
            ),
            "workspace_revision_after": (
                checkpoint.workspace_revision_after if checkpoint is not None else None
            ),
            "profile_status": profile.status if profile is not None else None,
            "profile_id": profile.profile_id if profile is not None else None,
            "result_status": result.status if result is not None else None,
            "outbox_state": outbox.state.value if outbox is not None else None,
            "last_error": checkpoint.last_error if checkpoint is not None else None,
            "timing": timing,
        }
        if output_json:
            _base._print_json(payload)
        else:
            print(
                "Edit "
                f"{command.command_id}: command={payload['command_state']} "
                f"checkpoint={payload['checkpoint_state']} "
                f"profile={payload['profile_status']} "
                f"outbox={payload['outbox_state']}"
            )
            end_to_end_ms = timing["durations_ms"]["end_to_end_ms"]
            if end_to_end_ms is not None:
                print(f"- end_to_end_ms={end_to_end_ms}")
            if payload["last_error"]:
                print(f"- {payload['last_error']}")
        return 0
    except Exception as exc:
        return _base._error("Edit status failed", exc)
    finally:
        if journal is not None:
            journal.close()


def main() -> None:
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] != "bridge" or argv[1] != "edit":
        _ORIGINAL_MAIN()
        return
    args = _parser().parse_args(argv)
    try:
        config = _base._load(args.config)
    except Exception as exc:
        sys.exit(_base._error("Failed to load config", exc))
    sys.exit(_edit_status(config, args.command_id, args.json))


def install_multi_file_patch_cli() -> None:
    _base._parser = _parser
    _base.main = main
    _legacy.main = main
