from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable

from .code_relationship_models import (
    ANALYSIS_VERSION, AnalysisImport, Confidence, DependencyEdge, EdgeKind, ImportKind,
    ReferenceKind, RepositoryAnalysis, ResolutionStatus, SymbolReference,
)
from .git_object_reader import GitObjectReader
from .protocol import BridgeError, sanitize_diagnostics
from .repository_index_models import ParseStatus, RepositoryFile, RepositorySnapshot, RepositorySymbol


@dataclass(frozen=True)
class _Resolution:
    target_path: str | None
    target_symbol_id: str | None
    status: ResolutionStatus
    confidence: Confidence
    diagnostic: str | None = None


@dataclass(frozen=True)
class _Binding:
    resolution: _Resolution
    module_name: str | None = None


@dataclass
class _Scope:
    qname: str
    symbol_id: str | None
    kind: str
    parent: _Scope | None
    bindings: dict[str, _Binding] = field(default_factory=dict)
    shadowed: set[str] = field(default_factory=set)
    children: dict[tuple[str, int], _Scope] = field(default_factory=dict)

    def bind(self, name: str, binding: _Binding) -> None:
        previous = self.bindings.get(name)
        if previous is not None and previous != binding:
            binding = _Binding(_Resolution(None, None, ResolutionStatus.AMBIGUOUS, Confidence.NONE, "multiple static bindings"))
        self.bindings[name] = binding


class _Catalog:
    def __init__(self, snapshot: RepositorySnapshot) -> None:
        modules: dict[str, list[str]] = {}
        self.by_qname: dict[tuple[str, str], list[RepositorySymbol]] = {}
        self.top: dict[tuple[str, str], list[RepositorySymbol]] = {}
        self.children: dict[tuple[str, str], list[tuple[str, RepositorySymbol]]] = {}
        for file in snapshot.files:
            if file.language == "python":
                module = module_name_for_path(file.path)
                if module is not None:
                    modules.setdefault(module, []).append(file.path)
            for symbol in file.symbols:
                self.by_qname.setdefault((file.path, symbol.qualified_name), []).append(symbol)
                if symbol.parent_symbol_id is None:
                    self.top.setdefault((file.path, symbol.name), []).append(symbol)
                else:
                    self.children.setdefault((symbol.parent_symbol_id, symbol.name), []).append((file.path, symbol))
        self.modules = {name: tuple(sorted(paths)) for name, paths in modules.items()}

    def module(self, name: str) -> _Resolution:
        matches = self.modules.get(name, ())
        if len(matches) == 1:
            return _Resolution(matches[0], None, ResolutionStatus.RESOLVED, Confidence.EXACT)
        if len(matches) > 1:
            return _Resolution(None, None, ResolutionStatus.AMBIGUOUS, Confidence.NONE, "multiple module paths")
        return _Resolution(None, None, ResolutionStatus.EXTERNAL, Confidence.NONE, "external module")

    def imported(self, module_name: str, name: str) -> _Resolution:
        module = self.module(module_name)
        if module.status is not ResolutionStatus.RESOLVED or module.target_path is None:
            return _Resolution(None, None, ResolutionStatus.EXTERNAL, Confidence.NONE, "external import") if module.status is ResolutionStatus.EXTERNAL else module
        symbols = self.top.get((module.target_path, name), ())
        child_paths = self.modules.get(f"{module_name}.{name}" if module_name else name, ())
        count = len(symbols) + len(child_paths)
        if count == 1 and symbols:
            return _Resolution(module.target_path, symbols[0].symbol_id, ResolutionStatus.RESOLVED, Confidence.EXACT)
        if count == 1:
            return _Resolution(child_paths[0], None, ResolutionStatus.RESOLVED, Confidence.EXACT)
        if count > 1:
            return _Resolution(None, None, ResolutionStatus.AMBIGUOUS, Confidence.NONE, "multiple imported targets")
        return _Resolution(None, None, ResolutionStatus.UNRESOLVED, Confidence.NONE, "name not found in local module")

    def definition(self, path: str, qname: str, line: int) -> RepositorySymbol | None:
        matches = self.by_qname.get((path, qname), ())
        exact = [item for item in matches if item.start_line == line]
        return exact[0] if len(exact) == 1 else matches[0] if len(matches) == 1 else None

    def child(self, parent_id: str, name: str) -> _Resolution:
        matches = self.children.get((parent_id, name), ())
        if len(matches) == 1:
            path, symbol = matches[0]
            return _Resolution(path, symbol.symbol_id, ResolutionStatus.RESOLVED, Confidence.HIGH)
        if len(matches) > 1:
            return _Resolution(None, None, ResolutionStatus.AMBIGUOUS, Confidence.NONE, "multiple class members")
        return _Resolution(None, None, ResolutionStatus.UNRESOLVED, Confidence.NONE, "class member not found")


class RepositoryRelationshipAnalyzer:
    def __init__(self, *, repo_path, repository_id: str, now_fn: Callable[[], str]) -> None:
        self.reader = GitObjectReader(repo_path)
        self.repository_id = repository_id
        self.now_fn = now_fn

    def build(self, snapshot: RepositorySnapshot) -> RepositoryAnalysis:
        if snapshot.repository_id != self.repository_id:
            raise BridgeError("invalid_payload", "Snapshot repository identity mismatch")
        catalog, imports, references = _Catalog(snapshot), [], []
        for file in snapshot.files:
            if file.language != "python" or file.parse_status is not ParseStatus.OK or not file.is_text:
                continue
            try:
                source = self.reader.read_blob(file.object_sha).decode("utf-8", errors="strict")
                tree = ast.parse(source, filename=file.path, type_comments=False)
            except (UnicodeError, SyntaxError, ValueError) as exc:
                raise BridgeError("analysis_conflict", f"Indexed Python blob is not parseable: {sanitize_diagnostics(str(exc))}") from exc
            analyzer = _FileAnalyzer(self.repository_id, snapshot.commit_sha, file, tree, catalog, len(imports), len(references))
            file_imports, file_references = analyzer.analyze()
            imports.extend(file_imports)
            references.extend(file_references)
        edges: list[DependencyEdge] = []
        for item in imports:
            if item.resolution_status is ResolutionStatus.RESOLVED and item.resolved_path:
                edges.append(_edge(self.repository_id, snapshot.commit_sha, item.source_path, item.source_symbol_id, item.resolved_path, item.resolved_symbol_id, EdgeKind.IMPORT, item.confidence, None, len(edges)))
        for item in references:
            if item.resolution_status is ResolutionStatus.RESOLVED and item.target_path:
                kind = EdgeKind.CALL if item.reference_kind is ReferenceKind.CALL else EdgeKind.REFERENCE
                edges.append(_edge(self.repository_id, snapshot.commit_sha, item.source_path, item.source_symbol_id, item.target_path, item.target_symbol_id, kind, item.confidence, item.reference_id, len(edges)))
        return RepositoryAnalysis(
            repository_id=self.repository_id,
            commit_sha=snapshot.commit_sha,
            analysis_version=ANALYSIS_VERSION,
            analyzed_at=self.now_fn(),
            python_file_count=sum(f.language == "python" and f.parse_status is ParseStatus.OK for f in snapshot.files),
            import_count=len(imports),
            reference_count=len(references),
            resolved_reference_count=sum(r.resolution_status is ResolutionStatus.RESOLVED for r in references),
            call_edge_count=sum(e.edge_kind is EdgeKind.CALL for e in edges),
            dependency_edge_count=len(edges),
            imports=tuple(imports), references=tuple(references), edges=tuple(edges),
        )


class _FileAnalyzer:
    def __init__(self, repository_id: str, commit_sha: str, file: RepositoryFile, tree: ast.Module, catalog: _Catalog, import_start: int, reference_start: int) -> None:
        self.repository_id, self.commit_sha, self.file, self.tree, self.catalog = repository_id, commit_sha, file, tree, catalog
        self.module_name = module_name_for_path(file.path) or ""
        self.imports: list[AnalysisImport] = []
        self.references: list[SymbolReference] = []
        self.import_ordinal, self.reference_ordinal = import_start, reference_start
        self.root = _Scope("", None, "module", None)

    def analyze(self) -> tuple[tuple[AnalysisImport, ...], tuple[SymbolReference, ...]]:
        self._build(self.tree.body, self.root)
        _Visitor(self).statements(self.tree.body, self.root)
        return tuple(self.imports), tuple(self.references)

    def _build(self, body: list[ast.stmt], scope: _Scope) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qname = f"{scope.qname}.{node.name}".strip(".")
                symbol = self.catalog.definition(self.file.path, qname, int(node.lineno))
                if symbol:
                    scope.bind(node.name, _Binding(_Resolution(self.file.path, symbol.symbol_id, ResolutionStatus.RESOLVED, Confidence.EXACT)))
                child = _Scope(qname, symbol.symbol_id if symbol else None, "class" if isinstance(node, ast.ClassDef) else "function", scope)
                scope.children[(node.name, int(node.lineno))] = child
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child.shadowed.update(_argument_names(node.args))
                for statement in node.body:
                    self._assignment(statement, child)
                self._build(node.body, child)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._import(node, scope)
            else:
                self._assignment(node, scope)
                for block in compound_blocks(node):
                    self._build(block, scope)

    def _assignment(self, node: ast.stmt, scope: _Scope) -> None:
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets.append(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets.append(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets.extend(item.optional_vars for item in node.items if item.optional_vars is not None)
        for target in targets:
            scope.shadowed.update(_target_names(target))

    def _import(self, node: ast.Import | ast.ImportFrom, scope: _Scope) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolution = self.catalog.module(alias.name)
                binding_name = alias.asname or alias.name.split(".", 1)[0]
                scope.bind(binding_name, _Binding(resolution, alias.name if alias.asname else binding_name))
                self.imports.append(self._make_import(scope, ImportKind.IMPORT, alias.name, None, alias.asname, 0, node, resolution))
            return
        module_name = resolve_relative_module(self.module_name, self.file.path, node.module or "", node.level)
        for alias in node.names:
            resolution = _Resolution(None, None, ResolutionStatus.UNSUPPORTED, Confidence.NONE, "wildcard import") if alias.name == "*" else self.catalog.imported(module_name, alias.name)
            scope.bind(alias.asname or alias.name, _Binding(resolution))
            self.imports.append(self._make_import(scope, ImportKind.FROM_IMPORT, module_name, alias.name, alias.asname, node.level, node, resolution))

    def _make_import(self, scope: _Scope, kind: ImportKind, module: str, name: str | None, alias: str | None, level: int, node: ast.AST, resolution: _Resolution) -> AnalysisImport:
        ordinal = self.import_ordinal; self.import_ordinal += 1
        identity = [self.repository_id, self.commit_sha, self.file.path, scope.symbol_id or "", kind.value, module, name or "", alias or "", str(node.lineno), str(node.col_offset), str(ordinal)]
        return AnalysisImport(_hash(identity), self.file.path, scope.symbol_id, kind, module, name, alias, level, int(node.lineno), int(node.col_offset), resolution.target_path, resolution.target_symbol_id, resolution.status, resolution.confidence, _bounded(resolution.diagnostic), ordinal)

    def resolve(self, node: ast.AST, scope: _Scope) -> _Resolution:
        if isinstance(node, ast.Name):
            for current in _lexical_scopes(scope):
                if node.id in current.bindings:
                    return current.bindings[node.id].resolution
                if node.id in current.shadowed:
                    return _Resolution(None, None, ResolutionStatus.DYNAMIC, Confidence.NONE, "name is locally bound")
            return _Resolution(None, None, ResolutionStatus.UNRESOLVED, Confidence.NONE, "name not found")
        if isinstance(node, ast.Attribute):
            chain = attribute_chain(node)
            if chain:
                first, *rest = chain
                if first in {"self", "cls"} and len(rest) == 1:
                    class_scope = nearest_class(scope)
                    if class_scope and class_scope.symbol_id:
                        return self.catalog.child(class_scope.symbol_id, rest[0])
                binding = _lookup(first, scope)
                if binding and binding.module_name and rest:
                    module = ".".join((binding.module_name, *rest[:-1])) if len(rest) > 1 else binding.module_name
                    return self.catalog.imported(module, rest[-1])
                if binding and binding.resolution.status is ResolutionStatus.EXTERNAL:
                    return binding.resolution
            return _Resolution(None, None, ResolutionStatus.DYNAMIC, Confidence.NONE, "unknown attribute owner")
        return _Resolution(None, None, ResolutionStatus.DYNAMIC, Confidence.NONE, "dynamic expression")

    def reference(self, node: ast.AST, scope: _Scope, kind: ReferenceKind) -> None:
        ordinal = self.reference_ordinal; self.reference_ordinal += 1
        expression, resolution = expression_text(node), self.resolve(node, scope)
        start_line = int(getattr(node, "lineno", 1) or 1); end_line = int(getattr(node, "end_lineno", start_line) or start_line)
        start_column = int(getattr(node, "col_offset", 0) or 0); end_column = int(getattr(node, "end_col_offset", start_column) or start_column)
        identity = [self.repository_id, self.commit_sha, self.file.path, scope.symbol_id or "", kind.value, expression, str(start_line), str(end_line), str(start_column), str(end_column), str(ordinal)]
        self.references.append(SymbolReference(_hash(identity), self.file.path, scope.symbol_id, resolution.target_path, resolution.target_symbol_id, kind, expression, start_line, end_line, start_column, end_column, resolution.status, resolution.confidence, _bounded(resolution.diagnostic), ordinal))


class _Visitor:
    def __init__(self, analyzer: _FileAnalyzer) -> None:
        self.a = analyzer

    def statements(self, body: list[ast.stmt], scope: _Scope) -> None:
        for node in body:
            self.statement(node, scope)

    def statement(self, node: ast.stmt, scope: _Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for item in node.decorator_list: self.a.reference(item, scope, ReferenceKind.DECORATOR)
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.annotation: self.a.reference(arg.annotation, scope, ReferenceKind.ANNOTATION)
            for arg in (node.args.vararg, node.args.kwarg):
                if arg and arg.annotation: self.a.reference(arg.annotation, scope, ReferenceKind.ANNOTATION)
            if node.returns: self.a.reference(node.returns, scope, ReferenceKind.ANNOTATION)
            for default in (*node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)): self.expression(default, scope)
            child = scope.children.get((node.name, int(node.lineno)))
            if child: self.statements(node.body, child)
            return
        if isinstance(node, ast.ClassDef):
            for item in node.decorator_list: self.a.reference(item, scope, ReferenceKind.DECORATOR)
            for item in node.bases: self.a.reference(item, scope, ReferenceKind.BASE_CLASS)
            for item in node.keywords: self.expression(item.value, scope)
            child = scope.children.get((node.name, int(node.lineno)))
            if child: self.statements(node.body, child)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr): self.expression(child, scope)
            elif isinstance(child, ast.stmt): self.statement(child, scope)

    def expression(self, node: ast.expr, scope: _Scope) -> None:
        if isinstance(node, ast.Call):
            self.a.reference(node.func, scope, ReferenceKind.CALL)
            for item in node.args: self.expression(item, scope)
            for item in node.keywords: self.expression(item.value, scope)
            return
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            self.a.reference(node, scope, ReferenceKind.ATTRIBUTE_READ); return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            self.a.reference(node, scope, ReferenceKind.NAME_READ); return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr): self.expression(child, scope)


def module_name_for_path(path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.suffix.lower() not in {".py", ".pyi"}: return None
    parts = list(pure.parts)
    if parts[-1] in {"__init__.py", "__init__.pyi"}: parts = parts[:-1]
    else: parts[-1] = pure.stem
    return ".".join(parts)


def resolve_relative_module(current_module: str, current_path: str, requested: str, level: int) -> str:
    if level <= 0: return requested
    package = current_module if PurePosixPath(current_path).name.startswith("__init__.") else current_module.rpartition(".")[0]
    parts = [item for item in package.split(".") if item]
    drop = level - 1
    if drop > len(parts): return requested
    base = parts[: len(parts) - drop]
    if requested: base.extend(requested.split("."))
    return ".".join(base)


def compound_blocks(node: ast.stmt) -> tuple[list[ast.stmt], ...]:
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)): return node.body, node.orelse
    if isinstance(node, (ast.With, ast.AsyncWith)): return (node.body,)
    if isinstance(node, (ast.Try, ast.TryStar)): return (node.body, *(h.body for h in node.handlers), node.orelse, node.finalbody)
    if isinstance(node, ast.Match): return tuple(case.body for case in node.cases)
    return ()


def _argument_names(args: ast.arguments) -> set[str]:
    result = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg: result.add(args.vararg.arg)
    if args.kwarg: result.add(args.kwarg.arg)
    return result


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name): return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        result: set[str] = set()
        for item in target.elts: result.update(_target_names(item))
        return result
    return set()


def attribute_chain(node: ast.Attribute) -> tuple[str, ...] | None:
    parts, value = [node.attr], node.value
    while isinstance(value, ast.Attribute): parts.append(value.attr); value = value.value
    if not isinstance(value, ast.Name): return None
    parts.append(value.id)
    return tuple(reversed(parts))


def nearest_class(scope: _Scope) -> _Scope | None:
    while scope:
        if scope.kind == "class": return scope
        scope = scope.parent
    return None


def _lexical_scopes(scope: _Scope):
    current: _Scope | None = scope
    while current:
        yield current
        current = current.parent
        while current is not None and current.kind == "class":
            current = current.parent


def _lookup(name: str, scope: _Scope) -> _Binding | None:
    for current in _lexical_scopes(scope):
        if name in current.bindings: return current.bindings[name]
        if name in current.shadowed: return None
    return None


def expression_text(node: ast.AST) -> str:
    try: value = ast.unparse(node)
    except Exception: value = type(node).__name__
    return (" ".join(value.split())[:1024] or type(node).__name__)


def _bounded(value: str | None) -> str | None:
    return sanitize_diagnostics(value, limit=500) if value else None


def _hash(parts: list[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _edge(repository_id: str, commit_sha: str, source_path: str, source_symbol_id: str | None, target_path: str, target_symbol_id: str | None, kind: EdgeKind, confidence: Confidence, origin: str | None, ordinal: int) -> DependencyEdge:
    identity = [repository_id, commit_sha, source_path, source_symbol_id or "", target_path, target_symbol_id or "", kind.value, origin or "", str(ordinal)]
    return DependencyEdge(_hash(identity), source_path, source_symbol_id, target_path, target_symbol_id, kind, ResolutionStatus.RESOLVED, confidence if confidence is not Confidence.NONE else Confidence.HEURISTIC, origin, ordinal)
