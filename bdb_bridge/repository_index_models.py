from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


INDEXER_VERSION = "ghb1a-v1"
MAX_PARSE_BYTES = 1 * 1024 * 1024
MAX_DOCSTRING_SUMMARY = 240
MAX_PARSE_DIAGNOSTIC = 500
GIT_COMMAND_TIMEOUT_SECONDS = 30


class FileKind(str, Enum):
    REGULAR = "regular"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"


class ParseStatus(str, Enum):
    OK = "ok"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    SYNTAX_ERROR = "syntax_error"
    TOO_LARGE = "too_large"
    BINARY = "binary"
    METADATA_ONLY = "metadata_only"


class SymbolKind(str, Enum):
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"
    ASYNC_METHOD = "async_method"
    NESTED_FUNCTION = "nested_function"
    NESTED_CLASS = "nested_class"


@dataclass(frozen=True)
class RepositorySymbol:
    symbol_id: str
    parent_symbol_id: str | None
    kind: SymbolKind
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    signature: str | None
    decorators: tuple[str, ...]
    docstring_summary: str | None
    ordinal: int


@dataclass(frozen=True)
class RepositoryFile:
    path: str
    git_mode: str
    git_object_type: str
    object_sha: str
    size_bytes: int
    content_sha256: str
    file_kind: FileKind
    language: str
    is_text: bool
    line_count: int | None
    parse_status: ParseStatus
    parse_diagnostic: str | None
    symbols: tuple[RepositorySymbol, ...] = ()


@dataclass(frozen=True)
class RepositorySnapshot:
    repository_id: str
    commit_sha: str
    tree_sha: str
    indexed_at: str
    file_count: int
    text_file_count: int
    binary_file_count: int
    python_file_count: int
    symbol_count: int
    indexer_version: str
    files: tuple[RepositoryFile, ...] = ()


@dataclass(frozen=True)
class IndexPersistOutcome:
    snapshot: RepositorySnapshot
    created: bool
    idempotent: bool


@dataclass(frozen=True)
class RepositoryIndexStatus:
    repository_id: str
    ref: str
    commit_sha: str
    tree_sha: str
    indexed: bool
    snapshot: RepositorySnapshot | None = None


@dataclass
class OutlineNode:
    symbol_id: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    signature: str | None
    decorators: list[str] = field(default_factory=list)
    docstring_summary: str | None = None
    ordinal: int = 0
    children: list[OutlineNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "children": [child.to_dict() for child in self.children],
            "decorators": list(self.decorators),
            "docstring_summary": self.docstring_summary,
            "end_column": self.end_column,
            "end_line": self.end_line,
            "kind": self.kind,
            "name": self.name,
            "ordinal": self.ordinal,
            "qualified_name": self.qualified_name,
            "signature": self.signature,
            "start_column": self.start_column,
            "start_line": self.start_line,
            "symbol_id": self.symbol_id,
        }


@dataclass(frozen=True)
class FileOutline:
    repository_id: str
    commit_sha: str
    path: str
    language: str
    parse_status: str
    parse_diagnostic: str | None
    file: RepositoryFile
    symbols: tuple[OutlineNode, ...]
