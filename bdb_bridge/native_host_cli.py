from __future__ import annotations

import sys
from pathlib import Path

from . import cli as _legacy
from . import ghb07_cli as _base
from .native_host import NativeArmStore, NativeHostConfig, default_native_config_path
from .runtime_hardening import install_runtime_hardening


install_runtime_hardening()

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
    native = commands.add_parser("native-host")
    native_commands = native.add_subparsers(dest="native_command", required=True)

    arm = native_commands.add_parser("arm")
    arm.add_argument("--config")
    arm.add_argument("--minutes", type=int, default=10)

    disarm = native_commands.add_parser("disarm")
    disarm.add_argument("--config")

    status = native_commands.add_parser("status")
    status.add_argument("--config")
    status.add_argument("--json", action="store_true")

    validate = native_commands.add_parser("validate-config")
    validate.add_argument("--config")
    return parser


def _config_path(value: str | None) -> Path:
    return Path(value).expanduser().resolve(strict=False) if value else default_native_config_path()


def _payload(status: object) -> dict[str, object]:
    return {
        "armed": bool(status.armed),
        "armed_until": status.armed_until,
        "generation_id": status.generation_id,
    }


def main() -> None:
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] != "bridge" or argv[1] != "native-host":
        _PREVIOUS_MAIN()
        return
    args = _parser().parse_args(argv)
    try:
        config = NativeHostConfig.from_json(_config_path(args.config))
        store = NativeArmStore(config.state_path)
        if args.native_command == "arm":
            result = _payload(store.arm(minutes=args.minutes))
            _base._print_json(result)
            code = 0
        elif args.native_command == "disarm":
            result = _payload(store.disarm())
            _base._print_json(result)
            code = 0
        elif args.native_command == "status":
            result = _payload(store.status())
            if args.json:
                _base._print_json(result)
            else:
                print(
                    "Native host: "
                    f"armed={str(result['armed']).lower()} "
                    f"armed_until={result['armed_until'] or '-'}"
                )
            code = 0
        else:
            _base._print_json(
                {
                    "valid": True,
                    "allowed_origins": len(config.allowed_origins),
                    "max_wait_seconds": config.max_wait_seconds,
                    "max_message_bytes": config.max_message_bytes,
                }
            )
            code = 0
    except Exception as exc:
        code = _base._error("Native host command failed", exc)
    sys.exit(code)


def install_native_host_cli() -> None:
    _base._parser = _parser
    _base.main = main
    _legacy.main = main
