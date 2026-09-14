"""CLI for explicit, fail-closed Project Memory recovery from accepted repository history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .composition import default_vnext_runtime_root
from .project_history_reconciliation import ProjectHistoryReconciler, ProjectHistoryReconciliationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or apply bounded reconciliation of Project Memory from BDB accepted commits."
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--runtime-root", default=str(default_vnext_runtime_root()))
    parser.add_argument("--ref", default="origin/main", dest="source_ref")
    parser.add_argument("--fetch-origin", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Required with --apply; confirms the explicit recovery mutation.")
    parser.add_argument(
        "--no-align-repo",
        action="store_true",
        help="Do not fast-forward the configured project checkout. Apply then fails if alignment is required.",
    )
    return parser


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.apply and not args.yes:
        _print(
            {
                "schema": "bdb-project-history-reconciliation-v1",
                "status": "FAILED",
                "error_code": "explicit_confirmation_required",
                "error": "--apply requires --yes",
            }
        )
        return 2

    try:
        reconciler = ProjectHistoryReconciler(Path(args.runtime_root), args.project_id)
        preview = reconciler.preview(source_ref=args.source_ref, fetch_origin=args.fetch_origin)
        if not args.apply:
            value = preview.to_dict()
            value["status"] = "PREVIEW"
            _print(value)
            return 0
        receipt = reconciler.apply(preview, align_repo=not args.no_align_repo)
        _print(receipt.to_dict())
        return 0
    except ProjectHistoryReconciliationError as exc:
        _print(
            {
                "schema": "bdb-project-history-reconciliation-v1",
                "status": "FAILED",
                "error_code": exc.code,
                "error": str(exc),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
