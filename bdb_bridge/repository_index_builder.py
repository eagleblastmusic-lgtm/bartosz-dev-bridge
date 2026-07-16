from __future__ import annotations

from pathlib import Path

from .file_classifier import classify_content
from .git_object_reader import GitObjectReader
from .protocol import BridgeError, parse_strict_utc_timestamp
from .python_symbol_parser import parse_python_symbols
from .repository_index_models import (
    INDEXER_VERSION,
    MAX_PARSE_BYTES,
    FileKind,
    ParseStatus,
    RepositoryFile,
    RepositorySnapshot,
)


class RepositoryIndexBuilder:
    def __init__(
        self,
        *,
        repo_path: Path,
        repository_id: str,
        now_fn,
        max_parse_bytes: int = MAX_PARSE_BYTES,
        reader: GitObjectReader | None = None,
    ) -> None:
        if not isinstance(repository_id, str) or not repository_id or len(repository_id) > 128:
            raise BridgeError("invalid_config", "repository_id must be a non-empty string")
        self._repository_id = repository_id
        self._now_fn = now_fn
        self._max_parse_bytes = max_parse_bytes
        self._reader = reader or GitObjectReader(repo_path)

    def build(self, ref: str = "HEAD") -> RepositorySnapshot:
        self._reader.ensure_repository()
        commit_sha = self._reader.resolve_commit(ref)
        tree_sha = self._reader.resolve_tree(commit_sha)
        indexed_at = self._now_fn()
        parse_strict_utc_timestamp(indexed_at, field="indexed_at")

        entries = self._reader.list_tree(commit_sha)
        files: list[RepositoryFile] = []
        text_file_count = 0
        binary_file_count = 0
        python_file_count = 0
        symbol_count = 0

        for entry in entries:
            data = b""
            size_bytes = entry.size_bytes
            if entry.file_kind is FileKind.SUBMODULE:
                data = entry.object_sha.encode("ascii")
                size_bytes = 0
            elif entry.file_kind is FileKind.SYMLINK:
                data = self._reader.read_blob(entry.object_sha)
                size_bytes = len(data)
            else:
                data = self._reader.read_blob(entry.object_sha)
                size_bytes = len(data)

            content_sha256 = self._reader.content_sha256(data)
            language, is_text, line_count, parse_status, parse_diagnostic = classify_content(
                path=entry.path,
                data=data,
                file_kind=entry.file_kind,
                max_parse_bytes=self._max_parse_bytes,
            )
            symbols: tuple = ()
            if parse_status is ParseStatus.OK and language == "python":
                parsed = parse_python_symbols(
                    source=data.decode("utf-8"),
                    repository_id=self._repository_id,
                    commit_sha=commit_sha,
                    path=entry.path,
                )
                parse_status = parsed.parse_status
                parse_diagnostic = parsed.parse_diagnostic
                symbols = parsed.symbols

            if is_text:
                text_file_count += 1
            else:
                binary_file_count += 1
            if language == "python" and entry.file_kind is FileKind.REGULAR:
                python_file_count += 1
            symbol_count += len(symbols)

            files.append(
                RepositoryFile(
                    path=entry.path,
                    git_mode=entry.mode,
                    git_object_type=entry.object_type,
                    object_sha=entry.object_sha,
                    size_bytes=size_bytes,
                    content_sha256=content_sha256,
                    file_kind=entry.file_kind,
                    language=language,
                    is_text=is_text,
                    line_count=line_count,
                    parse_status=parse_status,
                    parse_diagnostic=parse_diagnostic,
                    symbols=symbols,
                )
            )

        return RepositorySnapshot(
            repository_id=self._repository_id,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            indexed_at=indexed_at,
            file_count=len(files),
            text_file_count=text_file_count,
            binary_file_count=binary_file_count,
            python_file_count=python_file_count,
            symbol_count=symbol_count,
            indexer_version=INDEXER_VERSION,
            files=tuple(files),
        )
