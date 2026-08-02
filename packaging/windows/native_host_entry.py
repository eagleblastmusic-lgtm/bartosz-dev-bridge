from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

os.environ["BDB_LIGHTWEIGHT_NATIVE_HOST"] = "1"

from bdb_bridge.native_host import default_native_config_path
from bdb_bridge.native_host_project_launcher import run_project_launcher_host
from bdb_bridge.native_runtime_bootstrap import install_native_runtime
from bdb_bridge.windows_stdio import resolve_native_binary_stdio


install_native_runtime()


_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="BDB-Native-Host")
    parser.add_argument("origin", nargs="?")
    parser.add_argument("--parent-window")
    parser.add_argument("--config")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    origin = args.origin
    if not isinstance(origin, str) or _ORIGIN_RE.fullmatch(origin) is None:
        raise SystemExit(2)
    config_path = (
        Path(args.config).expanduser().resolve(strict=False)
        if args.config
        else default_native_config_path()
    )
    try:
        input_stream, output_stream = resolve_native_binary_stdio()
        code = run_project_launcher_host(
            config_path=config_path,
            origin=origin,
            input_stream=input_stream,
            output_stream=output_stream,
        )
    except Exception:
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
