from __future__ import annotations

import json
import sys
from pathlib import Path

from . import cli as _legacy
from . import ghb07_cli as _base
from .local_spool_transport import LocalSpoolWriter
from .protocol import BridgeError


_PREVIOUS_MAIN = _base.main
_PREVIOUS_PARSER = _base._parser


def _parser():
    parser = _PREVIOUS_PARSER()
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
    local = commands.add_parser("local")
    local_commands = local.add_subparsers(dest="local_command", required=True)

    submit = local_commands.add_parser("submit")
    submit.add_argument("--config", required=True)
    submit.add_argument("--envelope", required=True)
    submit.add_argument("--filename", required=True)

    status = local_commands.add_parser("status")
    status.add_argument("--config", required=True)
    status.add_argument("--json", action="store_true")
    return parser


def _submit(config: object, envelope_path: str, filename: str) -> int:
    try:
        path = Path(envelope_path).expanduser().resolve(strict=True)
        if not path.is_file() or path.is_symlink():
            raise BridgeError("unsafe_path", "Envelope must be a regular local file")
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise BridgeError("invalid_payload", "Envelope file is too large")
        decoded = raw.decode("utf-8", errors="strict")
        envelope = json.loads(decoded)
        if not isinstance(envelope, dict):
            raise BridgeError("invalid_payload", "Envelope root must be an object")
        destination = LocalSpoolWriter(config.direct_spool_dir).submit(
            envelope,
            filename=filename,
        )
        _base._print_json(
            {
                "accepted": True,
                "filename": destination.name,
                "spool": "direct-local",
            }
        )
        return 0
    except Exception as exc:
        return _base._error("Local submit failed", exc)


def _status(config: object, output_json: bool) -> int:
    try:
        inbox = Path(config.direct_spool_dir)
        files = sorted(path.name for path in inbox.glob("*.json")) if inbox.exists() else []
        payload = {
            "enabled": bool(config.direct_spool_enabled),
            "pending_envelopes": len(files),
            "files": files,
        }
        if output_json:
            _base._print_json(payload)
        else:
            print(
                "Direct local spool: "
                f"enabled={str(payload['enabled']).lower()} "
                f"pending_envelopes={payload['pending_envelopes']}"
            )
        return 0
    except Exception as exc:
        return _base._error("Local status failed", exc)


def main() -> None:
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] != "bridge" or argv[1] != "local":
        _PREVIOUS_MAIN()
        return
    args = _parser().parse_args(argv)
    try:
        config = _base._load(args.config)
    except Exception as exc:
        sys.exit(_base._error("Failed to load config", exc))
    if args.local_command == "submit":
        code = _submit(config, args.envelope, args.filename)
    else:
        code = _status(config, args.json)
    sys.exit(code)


def install_local_spool_cli() -> None:
    _base._parser = _parser
    _base.main = main
    _legacy.main = main
