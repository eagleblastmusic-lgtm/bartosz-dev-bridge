"""CLI for explicit retryable preflight recovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .retryable_preflight_recovery import RetryablePreflightRecovery, RetryablePreflightRecoveryError
from .runtime_authority import RuntimeAuthorityError, resolve_control_center_runtime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview/apply bounded recovery for a blocked HEAD-mismatch preflight attempt",
    )
    parser.add_argument("--runtime-root", default=None, help="Canonical runtime root; defaults to active Native route authority")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--fetch-upstream", action="store_true", help="Fetch only the configured upstream branch before preview")
    parser.add_argument("--apply", action="store_true", help="Apply the validated recovery")
    parser.add_argument("--yes", action="store_true", help="Required together with --apply")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.apply and not args.yes:
        print("BDB_RETRY_RECOVERY_ERROR:explicit_confirmation_required:--apply requires --yes")
        return 2
    try:
        if args.runtime_root:
            runtime_root = Path(args.runtime_root).expanduser().absolute()
        else:
            runtime_root = resolve_control_center_runtime().runtime_root
        recovery = RetryablePreflightRecovery(runtime_root, args.project_id)
        preview = recovery.preview(fetch_upstream=bool(args.fetch_upstream))
        if args.apply:
            result = recovery.apply(preview).to_dict()
        else:
            result = {**preview.to_dict(), "status": "PREVIEW"}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (RetryablePreflightRecoveryError, RuntimeAuthorityError) as exc:
        print(f"BDB_RETRY_RECOVERY_ERROR:{getattr(exc, 'code', 'recovery_failed')}:{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
