from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bdb_poc import *  # re-export the tested POC API for the single entry-point script


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bartosz Dev Bridge minimal POC-0")
    parser.add_argument("--config", type=Path, required=True, help="Path to local POC config JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = BridgeConfig.from_json(args.config)
        return PocBridge(config).run()
    except BridgeError as exc:
        print(f"BDB ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
