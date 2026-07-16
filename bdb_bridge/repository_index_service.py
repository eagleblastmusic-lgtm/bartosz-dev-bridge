from __future__ import annotations

from pathlib import Path

from .config import BridgeConfig
from .protocol import BridgeError, validate_repo_relative_path
from .repository_index_builder import RepositoryIndexBuilder
from .repository_index_models import (
    FileOutline,
    IndexPersistOutcome,
    OutlineNode,
    RepositoryFile,
    RepositoryIndexStatus,
    RepositorySnapshot,
    RepositorySymbol,
)


class RepositoryIndexService:
    def __init__(self, config: BridgeConfig, journal: object) -> None:
        self._config = config
        self._journal = journal

    def index(self, ref: str = "HEAD") -> IndexPersistOutcome:
        builder = RepositoryIndexBuilder(
            repo_path=Path(self._config.fixture_repo_path),
            repository_id=self._config.repository_id,
            now_fn=self._journal._now_fn,
        )
        snapshot = builder.build(ref)
        return self._journal.save_repository_snapshot(snapshot)

    def status(self, ref: str = "HEAD") -> RepositoryIndexStatus:
        builder = RepositoryIndexBuilder(
            repo_path=Path(self._config.fixture_repo_path),
            repository_id=self._config.repository_id,
            now_fn=self._journal._now_fn,
        )
        builder._reader.ensure_repository()
        commit_sha = builder._reader.resolve_commit(ref)
        tree_sha = builder._reader.resolve_tree(commit_sha)
        snapshot = self._journal.get_repository_snapshot(self._config.repository_id, commit_sha)
        return RepositoryIndexStatus(
            repository_id=self._config.repository_id,
            ref=ref,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            indexed=snapshot is not None,
            snapshot=snapshot,
        )

    def files(self, ref: str = "HEAD") -> tuple[RepositorySnapshot, tuple[RepositoryFile, ...]]:
        status = self.status(ref)
        if not status.indexed or status.snapshot is None:
            raise BridgeError("not_found", "Repository snapshot is not indexed for the resolved commit")
        files = self._journal.list_repository_files(
            self._config.repository_id,
            status.commit_sha,
            include_symbols=False,
        )
        return status.snapshot, files

    def outline(self, path: str, ref: str = "HEAD") -> FileOutline:
        path = validate_repo_relative_path(path)
        status = self.status(ref)
        if not status.indexed:
            raise BridgeError("not_found", "Repository snapshot is not indexed for the resolved commit")
        file_record = self._journal.get_repository_file(
            self._config.repository_id,
            status.commit_sha,
            path,
            include_symbols=True,
        )
        if file_record is None:
            raise BridgeError("not_found", f"Path not found in repository snapshot: {path}")
        tree = _build_outline_tree(file_record.symbols)
        return FileOutline(
            repository_id=self._config.repository_id,
            commit_sha=status.commit_sha,
            path=path,
            language=file_record.language,
            parse_status=file_record.parse_status.value,
            parse_diagnostic=file_record.parse_diagnostic,
            file=file_record,
            symbols=tree,
        )


def _build_outline_tree(symbols: tuple[RepositorySymbol, ...]) -> tuple[OutlineNode, ...]:
    nodes: dict[str, OutlineNode] = {}
    roots: list[OutlineNode] = []
    for symbol in symbols:
        node = OutlineNode(
            symbol_id=symbol.symbol_id,
            kind=symbol.kind.value,
            name=symbol.name,
            qualified_name=symbol.qualified_name,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            start_column=symbol.start_column,
            end_column=symbol.end_column,
            signature=symbol.signature,
            decorators=list(symbol.decorators),
            docstring_summary=symbol.docstring_summary,
            ordinal=symbol.ordinal,
            children=[],
        )
        nodes[symbol.symbol_id] = node
    for symbol in symbols:
        node = nodes[symbol.symbol_id]
        parent_id = symbol.parent_symbol_id
        if parent_id and parent_id in nodes:
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)
    return tuple(roots)


def snapshot_summary_dict(snapshot: RepositorySnapshot) -> dict[str, object]:
    return {
        "binary_file_count": snapshot.binary_file_count,
        "commit_sha": snapshot.commit_sha,
        "file_count": snapshot.file_count,
        "indexed_at": snapshot.indexed_at,
        "indexer_version": snapshot.indexer_version,
        "python_file_count": snapshot.python_file_count,
        "repository_id": snapshot.repository_id,
        "symbol_count": snapshot.symbol_count,
        "text_file_count": snapshot.text_file_count,
        "tree_sha": snapshot.tree_sha,
    }


def file_dict(file_record: RepositoryFile) -> dict[str, object]:
    return {
        "content_sha256": file_record.content_sha256,
        "file_kind": file_record.file_kind.value,
        "git_mode": file_record.git_mode,
        "git_object_type": file_record.git_object_type,
        "is_text": file_record.is_text,
        "language": file_record.language,
        "line_count": file_record.line_count,
        "object_sha": file_record.object_sha,
        "parse_diagnostic": file_record.parse_diagnostic,
        "parse_status": file_record.parse_status.value,
        "path": file_record.path,
        "size_bytes": file_record.size_bytes,
    }
