from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass

from .protocol import sanitize_diagnostics
from .repository_index_models import (
    MAX_DOCSTRING_SUMMARY,
    MAX_PARSE_DIAGNOSTIC,
    ParseStatus,
    RepositorySymbol,
    SymbolKind,
)


@dataclass(frozen=True)
class PythonParseResult:
    parse_status: ParseStatus
    parse_diagnostic: str | None
    symbols: tuple[RepositorySymbol, ...]


def parse_python_symbols(
    *,
    source: str,
    repository_id: str,
    commit_sha: str,
    path: str,
) -> PythonParseResult:
    try:
        tree = ast.parse(source, filename=path, type_comments=False)
    except SyntaxError as exc:
        detail = sanitize_diagnostics(
            f"syntax error line {exc.lineno or 0}: {exc.msg or 'invalid syntax'}",
            limit=MAX_PARSE_DIAGNOSTIC,
        )
        return PythonParseResult(ParseStatus.SYNTAX_ERROR, detail or "syntax error", ())
    except ValueError as exc:
        detail = sanitize_diagnostics(str(exc), limit=MAX_PARSE_DIAGNOSTIC) or "parse failed"
        return PythonParseResult(ParseStatus.SYNTAX_ERROR, detail, ())

    collector = _SymbolCollector(repository_id=repository_id, commit_sha=commit_sha, path=path)
    collector.visit_body(tree.body, parent_stack=())
    return PythonParseResult(ParseStatus.OK, None, tuple(collector.symbols))


class _SymbolCollector:
    def __init__(self, *, repository_id: str, commit_sha: str, path: str) -> None:
        self._repository_id = repository_id
        self._commit_sha = commit_sha
        self._path = path
        self.symbols: list[RepositorySymbol] = []
        self._ordinal = 0

    def visit_body(self, body: list[ast.stmt], *, parent_stack: tuple[str, ...], in_class: bool = False) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                self._handle_class(node, parent_stack=parent_stack, nested=bool(parent_stack))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._handle_function(node, parent_stack=parent_stack, in_class=in_class)

    def _handle_class(self, node: ast.ClassDef, *, parent_stack: tuple[str, ...], nested: bool) -> None:
        kind = SymbolKind.NESTED_CLASS if nested else SymbolKind.CLASS
        qualified = ".".join((*parent_stack, node.name))
        symbol = self._make_symbol(node=node, kind=kind, name=node.name, qualified_name=qualified, parent_stack=parent_stack)
        self.symbols.append(symbol)
        self.visit_body(node.body, parent_stack=(*parent_stack, node.name), in_class=True)

    def _handle_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        parent_stack: tuple[str, ...],
        in_class: bool,
    ) -> None:
        is_async = isinstance(node, ast.AsyncFunctionDef)
        if in_class:
            kind = SymbolKind.ASYNC_METHOD if is_async else SymbolKind.METHOD
        elif parent_stack:
            kind = SymbolKind.NESTED_FUNCTION
        else:
            kind = SymbolKind.ASYNC_FUNCTION if is_async else SymbolKind.FUNCTION

        qualified = ".".join((*parent_stack, node.name))
        symbol = self._make_symbol(
            node=node,
            kind=kind,
            name=node.name,
            qualified_name=qualified,
            parent_stack=parent_stack,
            signature=_format_signature(node),
        )
        self.symbols.append(symbol)
        self.visit_body(node.body, parent_stack=(*parent_stack, node.name), in_class=False)

    def _make_symbol(
        self,
        *,
        node: ast.AST,
        kind: SymbolKind,
        name: str,
        qualified_name: str,
        parent_stack: tuple[str, ...],
        signature: str | None = None,
    ) -> RepositorySymbol:
        start_line = int(getattr(node, "lineno", 1) or 1)
        end_line = int(getattr(node, "end_lineno", start_line) or start_line)
        start_column = int(getattr(node, "col_offset", 0) or 0)
        end_column = int(getattr(node, "end_col_offset", 0) or 0)
        decorators = _decorator_names(node)
        docstring_summary = _docstring_summary(node)
        parent_symbol_id = None
        if parent_stack:
            # parent is the most recently emitted symbol with matching qualified name
            parent_qname = ".".join(parent_stack)
            for existing in reversed(self.symbols):
                if existing.qualified_name == parent_qname:
                    parent_symbol_id = existing.symbol_id
                    break
        symbol_id = _symbol_id(
            repository_id=self._repository_id,
            commit_sha=self._commit_sha,
            path=self._path,
            kind=kind.value,
            qualified_name=qualified_name,
            start_line=start_line,
            end_line=end_line,
            start_column=start_column,
            end_column=end_column,
        )
        ordinal = self._ordinal
        self._ordinal += 1
        return RepositorySymbol(
            symbol_id=symbol_id,
            parent_symbol_id=parent_symbol_id,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_line=start_line,
            end_line=end_line,
            start_column=start_column,
            end_column=end_column,
            signature=signature,
            decorators=decorators,
            docstring_summary=docstring_summary,
            ordinal=ordinal,
        )


def _symbol_id(
    *,
    repository_id: str,
    commit_sha: str,
    path: str,
    kind: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
    start_column: int,
    end_column: int,
) -> str:
    payload = "|".join(
        [
            repository_id,
            commit_sha,
            path,
            kind,
            qualified_name,
            str(start_line),
            str(end_line),
            str(start_column),
            str(end_column),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decorator_names(node: ast.AST) -> tuple[str, ...]:
    decorator_list = getattr(node, "decorator_list", None)
    if not decorator_list:
        return ()
    names: list[str] = []
    for decorator in decorator_list:
        names.append(_expr_name(decorator))
    return tuple(names)


def _expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_expr_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _docstring_summary(node: ast.AST) -> str | None:
    body = getattr(node, "body", None)
    if not body:
        return None
    docstring = ast.get_docstring(node, clean=True)
    if not docstring:
        return None
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:MAX_DOCSTRING_SUMMARY]
    return None


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    parts: list[str] = []

    pos_only = list(args.posonlyargs)
    defaults = list(args.defaults)
    normal = list(args.args)
    positional = pos_only + normal
    default_offset = len(positional) - len(defaults)

    for index, arg in enumerate(positional):
        piece = arg.arg
        if arg.annotation is not None:
            piece = f"{piece}: {_anno(arg.annotation)}"
        default_index = index - default_offset
        if default_index >= 0:
            piece = f"{piece}={_anno(defaults[default_index])}"
        parts.append(piece)
        if pos_only and index == len(pos_only) - 1:
            parts.append("/")

    if args.vararg is not None:
        piece = f"*{args.vararg.arg}"
        if args.vararg.annotation is not None:
            piece = f"{piece}: {_anno(args.vararg.annotation)}"
        parts.append(piece)
    elif args.kwonlyargs:
        parts.append("*")

    for index, arg in enumerate(args.kwonlyargs):
        piece = arg.arg
        if arg.annotation is not None:
            piece = f"{piece}: {_anno(arg.annotation)}"
        default = args.kw_defaults[index]
        if default is not None:
            piece = f"{piece}={_anno(default)}"
        parts.append(piece)

    if args.kwarg is not None:
        piece = f"**{args.kwarg.arg}"
        if args.kwarg.annotation is not None:
            piece = f"{piece}: {_anno(args.kwarg.annotation)}"
        parts.append(piece)

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = ""
    if node.returns is not None:
        returns = f" -> {_anno(node.returns)}"
    signature = f"{prefix} {node.name}({', '.join(parts)}){returns}"
    return signature[:2048]


def _anno(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def decorators_json(decorators: tuple[str, ...]) -> str:
    return json.dumps(list(decorators), ensure_ascii=False, sort_keys=False, separators=(",", ":"))
