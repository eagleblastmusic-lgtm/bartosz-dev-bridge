from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .models import OperatorResponse
from .operator import OperatorApi


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bartosz Dev Bridge local Operator API v1")
    parser.add_argument(
        "--bridge-repo",
        default=str(Path(__file__).resolve().parents[1]),
        help="BDB implementation checkout containing scripts/Invoke-BDBWorkspaceLoop.ps1",
    )
    parser.add_argument("--powershell", default="powershell.exe")
    parser.add_argument("--timeout", type=float, default=60.0)

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")

    list_parser = commands.add_parser("list-projects")
    list_parser.add_argument("--workspaces-root", required=True)

    for name in ("status", "stop", "current-operation"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)

    events_parser = commands.add_parser("events")
    events_parser.add_argument("--root", required=True)
    events_parser.add_argument("--after-event-id", type=int, default=0)
    events_parser.add_argument("--limit", type=int, default=100)
    events_parser.add_argument("--session-id")
    events_parser.add_argument("--command-id")

    logs_parser = commands.add_parser("logs")
    logs_parser.add_argument("--root", required=True)
    logs_parser.add_argument("--max-bytes", type=int, default=65_536)
    logs_parser.add_argument("--max-lines", type=int, default=200)

    start_parser = commands.add_parser("start")
    start_parser.add_argument("--root", required=True)
    start_parser.add_argument("--arm-minutes", type=int, default=30)

    rearm_parser = commands.add_parser("rearm")
    rearm_parser.add_argument("--root", required=True)
    rearm_parser.add_argument("--arm-minutes", type=int, default=30)

    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--root", required=True)
    prepare_parser.add_argument("--repo", required=True)
    prepare_parser.add_argument("--alias", required=True)
    prepare_parser.add_argument("--allowed-path", action="append", default=[])
    prepare_parser.add_argument("--test-timeout", type=float, default=120.0)
    prepare_parser.add_argument("--python")
    return parser


def _execute(api: OperatorApi, args: argparse.Namespace) -> OperatorResponse:
    if args.command == "capabilities":
        return api.capabilities()
    if args.command == "list-projects":
        return api.list_projects(args.workspaces_root)
    if args.command == "status":
        return api.status(args.root)
    if args.command == "events":
        return api.events(
            args.root,
            after_event_id=args.after_event_id,
            limit=args.limit,
            session_id=args.session_id,
            command_id=args.command_id,
        )
    if args.command == "current-operation":
        return api.current_operation(args.root)
    if args.command == "logs":
        return api.logs(args.root, max_bytes=args.max_bytes, max_lines=args.max_lines)
    if args.command == "start":
        return api.start(args.root, arm_minutes=args.arm_minutes)
    if args.command == "stop":
        return api.stop(args.root)
    if args.command == "rearm":
        return api.rearm(args.root, arm_minutes=args.arm_minutes)
    if args.command == "prepare":
        return api.prepare(
            args.root,
            source_repo=args.repo,
            alias=args.alias,
            allowed_paths=args.allowed_path,
            test_timeout_seconds=args.test_timeout,
            python_executable=args.python,
        )
    raise AssertionError(f"Unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    api = OperatorApi(
        repo_root=args.bridge_repo,
        powershell_executable=args.powershell,
        default_timeout_seconds=args.timeout,
    )
    response = _execute(api, args)
    print(json.dumps(response.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
