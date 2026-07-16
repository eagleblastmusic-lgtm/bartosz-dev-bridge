from __future__ import annotations

import argparse

from .code_relationship_service import (
    RepositoryRelationshipService,
    analysis_summary_dict,
    reference_dict,
    search_result_dict,
    symbol_dict,
)
from .context_pack_service import (
    ContextPackService,
    context_pack_dict,
    gate_result_dict,
    render_context_markdown,
)


def _add_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol-id")
    parser.add_argument("--path")
    parser.add_argument("--qualified-name")


def add_relationship_parsers(repo_commands) -> None:
    analyze = repo_commands.add_parser("analyze")
    analyze.add_argument("--config", required=True)
    analyze.add_argument("--ref", default="HEAD")
    analyze.add_argument("--json", action="store_true")
    search = repo_commands.add_parser("search")
    search.add_argument("--config", required=True)
    search.add_argument("--ref", default="HEAD")
    search.add_argument("--query", required=True)
    search.add_argument("--kind", choices=("all", "file", "symbol"), default="all")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--json", action="store_true")
    references = repo_commands.add_parser("references")
    references.add_argument("--config", required=True)
    references.add_argument("--ref", default="HEAD")
    _add_selector(references)
    references.add_argument("--direction", choices=("incoming", "outgoing"), default="incoming")
    references.add_argument("--kind", choices=("all", "call", "name_read", "attribute_read", "decorator", "base_class", "annotation"), default="all")
    references.add_argument("--limit", type=int, default=100)
    references.add_argument("--json", action="store_true")
    callers = repo_commands.add_parser("callers")
    callers.add_argument("--config", required=True)
    callers.add_argument("--ref", default="HEAD")
    _add_selector(callers)
    callers.add_argument("--limit", type=int, default=100)
    callers.add_argument("--json", action="store_true")
    dependencies = repo_commands.add_parser("dependencies")
    dependencies.add_argument("--config", required=True)
    dependencies.add_argument("--ref", default="HEAD")
    dependencies.add_argument("--path", required=True)
    dependencies.add_argument("--direction", choices=("incoming", "outgoing"), default="outgoing")
    dependencies.add_argument("--depth", type=int, default=1)
    dependencies.add_argument("--edge-kind", choices=("all", "import", "call", "reference"), default="all")
    dependencies.add_argument("--max-nodes", type=int, default=200)
    dependencies.add_argument("--json", action="store_true")
    context = repo_commands.add_parser("context")
    context.add_argument("--config", required=True)
    context.add_argument("--ref", default="HEAD")
    context.add_argument("--query")
    _add_selector(context)
    context.add_argument("--direction", choices=("incoming", "outgoing", "both"), default="both")
    context.add_argument("--depth", type=int, default=2)
    context.add_argument("--max-files", type=int, default=20)
    context.add_argument("--max-bytes", type=int, default=64 * 1024)
    context.add_argument("--max-excerpt-lines", type=int, default=80)
    context.add_argument("--json", action="store_true")
    gate = repo_commands.add_parser("gate")
    gate.add_argument("--config", required=True)
    gate.add_argument("--ref", default="HEAD")
    gate.add_argument("--max-files", type=int, default=200_000)
    gate.add_argument("--max-symbols", type=int, default=2_000_000)
    gate.add_argument("--max-relationships", type=int, default=5_000_000)
    gate.add_argument("--sample-max-files", type=int, default=20)
    gate.add_argument("--sample-max-bytes", type=int, default=32 * 1024)
    gate.add_argument("--json", action="store_true")


def _selector(args: argparse.Namespace) -> dict[str, str | None]:
    return {"symbol_id": args.symbol_id, "path": args.path, "qualified_name": args.qualified_name}


def handle_relationship_command(config, args: argparse.Namespace, offline, print_json, error) -> int:
    journal = None
    lock = None
    try:
        journal, lock = offline(config)
        if args.repo_command in {"context", "gate"}:
            context_service = ContextPackService(config, journal)
            if args.repo_command == "context":
                pack = context_service.build(
                    ref=args.ref,
                    query=args.query,
                    **_selector(args),
                    direction=args.direction,
                    depth=args.depth,
                    max_files=args.max_files,
                    max_bytes=args.max_bytes,
                    max_excerpt_lines=args.max_excerpt_lines,
                )
                if args.json:
                    print_json(context_pack_dict(pack))
                else:
                    print(render_context_markdown(pack), end="")
            else:
                result = context_service.gate(
                    ref=args.ref,
                    max_files=args.max_files,
                    max_symbols=args.max_symbols,
                    max_relationships=args.max_relationships,
                    sample_max_files=args.sample_max_files,
                    sample_max_bytes=args.sample_max_bytes,
                )
                payload = gate_result_dict(result)
                if args.json:
                    print_json(payload)
                else:
                    print(f"Repository gate passed={str(result.passed).lower()} checks={len(result.checks)}")
                    for item in result.checks:
                        print(f"- {item.name}: {'PASS' if item.passed else 'FAIL'} ({item.detail})")
                return 0 if result.passed else 1
            return 0

        service = RepositoryRelationshipService(config, journal)
        if args.repo_command == "analyze":
            outcome = service.analyze(args.ref)
            payload = {**analysis_summary_dict(outcome.analysis), "created": outcome.created, "idempotent": outcome.idempotent}
            if args.json:
                print_json(payload)
            else:
                print(f"Analyzed {outcome.analysis.repository_id}@{outcome.analysis.commit_sha[:12]} imports={outcome.analysis.import_count} references={outcome.analysis.reference_count} idempotent={str(outcome.idempotent).lower()}")
        elif args.repo_command == "search":
            results = service.search(ref=args.ref, query=args.query, kind=args.kind, limit=args.limit)
            payload = {"kind": args.kind, "query": args.query.strip(), "results": [search_result_dict(item) for item in results]}
            if args.json:
                print_json(payload)
            else:
                for item in results:
                    print(f"{item.result_kind}: {item.path} {item.qualified_name or item.name}")
        elif args.repo_command == "references":
            selected, items = service.references(ref=args.ref, **_selector(args), direction=args.direction, reference_kind=args.kind, limit=args.limit)
            payload = {"direction": args.direction, "kind": args.kind, "references": [reference_dict(item) for item in items], "symbol": symbol_dict(*selected)}
            if args.json:
                print_json(payload)
            else:
                print(f"References: {len(items)}")
        elif args.repo_command == "callers":
            selected, items = service.callers(ref=args.ref, **_selector(args), limit=args.limit)
            payload = {"callers": [reference_dict(item) for item in items], "symbol": symbol_dict(*selected)}
            if args.json:
                print_json(payload)
            else:
                print(f"Callers: {len(items)}")
        elif args.repo_command == "dependencies":
            payload = service.dependencies(ref=args.ref, path=args.path, direction=args.direction, depth=args.depth, max_nodes=args.max_nodes, edge_kind=args.edge_kind)
            if args.json:
                print_json(payload)
            else:
                print(f"Dependencies nodes={len(payload['nodes'])} edges={len(payload['edges'])} truncated={str(payload['truncated']).lower()}")
        else:
            raise ValueError(f"Unsupported repo relationship command: {args.repo_command}")
        return 0
    except Exception as exc:
        return error(f"Repository {args.repo_command} failed", exc)
    finally:
        if journal is not None:
            journal.close()
        if lock is not None:
            lock.release()
