from __future__ import annotations

import argparse
import json
from pathlib import Path

from bdb_release import (
    create_release_manifest,
    load_release_manifest,
    verify_release_artifact,
    write_release_manifest,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Build or verify a BDB Control Center release manifest")
    sub = root.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--artifact", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--built-at")

    verify = sub.add_parser("verify")
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--manifest", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_release_manifest(
                args.artifact,
                version=args.version,
                source_commit=args.source_commit,
                built_at=args.built_at,
            )
            path = write_release_manifest(manifest, args.output)
            result = {"status": "success", "manifest": str(path), "document": manifest.to_dict()}
        else:
            manifest = load_release_manifest(args.manifest)
            receipt = verify_release_artifact(manifest, args.artifact)
            result = {"status": "success", "receipt": receipt.to_dict()}
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
