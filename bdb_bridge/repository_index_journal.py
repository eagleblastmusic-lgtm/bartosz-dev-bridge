from __future__ import annotations

import json
import sqlite3
from typing import Type

from .migrations import map_sqlite_error
from .protocol import BridgeError, parse_strict_utc_timestamp, sanitize_diagnostics, validate_repo_relative_path
from .python_symbol_parser import decorators_json
from .repository_index_models import (
    FileKind,
    IndexPersistOutcome,
    ParseStatus,
    RepositoryFile,
    RepositorySnapshot,
    RepositorySymbol,
    SymbolKind,
)

_SNAPSHOT_SELECT = """
SELECT repository_id, commit_sha, tree_sha, indexed_at, file_count, text_file_count,
       binary_file_count, python_file_count, symbol_count, indexer_version
FROM repository_snapshots
WHERE repository_id = ? AND commit_sha = ?
"""

_FILE_SELECT = """
SELECT path, git_mode, git_object_type, object_sha, size_bytes, content_sha256, file_kind,
       language, is_text, line_count, parse_status, parse_diagnostic
FROM repository_files
WHERE repository_id = ? AND commit_sha = ?
ORDER BY path ASC
"""

_SYMBOL_SELECT = """
SELECT path, symbol_id, parent_symbol_id, kind, name, qualified_name, start_line, end_line,
       start_column, end_column, signature, decorators_json, docstring_summary, ordinal
FROM repository_symbols
WHERE repository_id = ? AND commit_sha = ?
ORDER BY path ASC, ordinal ASC
"""

_FILE_ONE_SELECT = """
SELECT path, git_mode, git_object_type, object_sha, size_bytes, content_sha256, file_kind,
       language, is_text, line_count, parse_status, parse_diagnostic
FROM repository_files
WHERE repository_id = ? AND commit_sha = ? AND path = ?
"""

_SYMBOLS_FOR_PATH = """
SELECT path, symbol_id, parent_symbol_id, kind, name, qualified_name, start_line, end_line,
       start_column, end_column, signature, decorators_json, docstring_summary, ordinal
FROM repository_symbols
WHERE repository_id = ? AND commit_sha = ? AND path = ?
ORDER BY ordinal ASC
"""


def _validate_sha(value: str, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise BridgeError("invalid_payload", f"{field} must be a 40-character lowercase hex SHA")
    return value


def _snapshot_row(row: tuple[object, ...]) -> RepositorySnapshot:
    try:
        snapshot = RepositorySnapshot(
            repository_id=str(row[0]),
            commit_sha=_validate_sha(str(row[1]), field="commit_sha"),
            tree_sha=_validate_sha(str(row[2]), field="tree_sha"),
            indexed_at=str(row[3]),
            file_count=int(row[4]),
            text_file_count=int(row[5]),
            binary_file_count=int(row[6]),
            python_file_count=int(row[7]),
            symbol_count=int(row[8]),
            indexer_version=str(row[9]),
            files=(),
        )
        parse_strict_utc_timestamp(snapshot.indexed_at, field="indexed_at")
        if not snapshot.repository_id or not snapshot.indexer_version:
            raise ValueError("missing identity")
        for name in (
            "file_count",
            "text_file_count",
            "binary_file_count",
            "python_file_count",
            "symbol_count",
        ):
            if getattr(snapshot, name) < 0:
                raise ValueError("negative count")
        return snapshot
    except (BridgeError, ValueError, TypeError) as exc:
        if isinstance(exc, BridgeError) and exc.code == "journal_corrupt":
            raise
        raise BridgeError("journal_corrupt", "Invalid repository_snapshots row") from exc


def _parse_decorators(raw: str) -> tuple[str, ...]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BridgeError("journal_corrupt", "Invalid decorators_json") from exc
    if not isinstance(loaded, list) or any(not isinstance(item, str) for item in loaded):
        raise BridgeError("journal_corrupt", "Invalid decorators_json")
    return tuple(loaded)


def _file_row(row: tuple[object, ...], symbols: tuple[RepositorySymbol, ...] = ()) -> RepositoryFile:
    try:
        path = validate_repo_relative_path(str(row[0]))
        file_record = RepositoryFile(
            path=path,
            git_mode=str(row[1]),
            git_object_type=str(row[2]),
            object_sha=_validate_sha(str(row[3]), field="object_sha"),
            size_bytes=int(row[4]),
            content_sha256=str(row[5]),
            file_kind=FileKind(str(row[6])),
            language=str(row[7]),
            is_text=bool(int(row[8])),
            line_count=None if row[9] is None else int(row[9]),
            parse_status=ParseStatus(str(row[10])),
            parse_diagnostic=None if row[11] is None else str(row[11]),
            symbols=symbols,
        )
        if len(file_record.content_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in file_record.content_sha256
        ):
            raise ValueError("bad content hash")
        if file_record.size_bytes < 0:
            raise ValueError("bad size")
        if file_record.parse_diagnostic is not None and len(file_record.parse_diagnostic) > 500:
            raise ValueError("oversized diagnostic")
        return file_record
    except (BridgeError, ValueError, TypeError) as exc:
        if isinstance(exc, BridgeError) and exc.code in {"journal_corrupt", "unsafe_path"}:
            if exc.code == "unsafe_path":
                raise BridgeError("journal_corrupt", "Invalid repository_files path") from exc
            raise
        raise BridgeError("journal_corrupt", "Invalid repository_files row") from exc


def _symbol_row(row: tuple[object, ...]) -> tuple[str, RepositorySymbol]:
    try:
        path = validate_repo_relative_path(str(row[0]))
        symbol = RepositorySymbol(
            symbol_id=str(row[1]),
            parent_symbol_id=None if row[2] is None else str(row[2]),
            kind=SymbolKind(str(row[3])),
            name=str(row[4]),
            qualified_name=str(row[5]),
            start_line=int(row[6]),
            end_line=int(row[7]),
            start_column=int(row[8]),
            end_column=int(row[9]),
            signature=None if row[10] is None else str(row[10]),
            decorators=_parse_decorators(str(row[11])),
            docstring_summary=None if row[12] is None else str(row[12]),
            ordinal=int(row[13]),
        )
        if len(symbol.symbol_id) != 64:
            raise ValueError("bad symbol id")
        if symbol.start_line < 1 or symbol.end_line < 1 or symbol.ordinal < 0:
            raise ValueError("bad ranges")
        return path, symbol
    except (BridgeError, ValueError, TypeError) as exc:
        if isinstance(exc, BridgeError) and exc.code == "journal_corrupt":
            raise
        raise BridgeError("journal_corrupt", "Invalid repository_symbols row") from exc


def get_repository_snapshot(
    journal: object,
    repository_id: str,
    commit_sha: str,
    *,
    include_files: bool = False,
    include_symbols: bool = False,
) -> RepositorySnapshot | None:
    journal._ensure_open()
    if not isinstance(repository_id, str) or not repository_id:
        raise BridgeError("invalid_payload", "repository_id must be a non-empty string")
    commit_sha = _validate_sha(commit_sha.lower(), field="commit_sha")
    try:
        row = journal._connection.execute(_SNAPSHOT_SELECT, (repository_id, commit_sha)).fetchone()
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="repository snapshot read") from exc
    if row is None:
        return None
    snapshot = _snapshot_row(row)
    if not include_files and not include_symbols:
        return snapshot
    files = list_repository_files(
        journal,
        repository_id,
        commit_sha,
        include_symbols=include_symbols,
    )
    return RepositorySnapshot(
        repository_id=snapshot.repository_id,
        commit_sha=snapshot.commit_sha,
        tree_sha=snapshot.tree_sha,
        indexed_at=snapshot.indexed_at,
        file_count=snapshot.file_count,
        text_file_count=snapshot.text_file_count,
        binary_file_count=snapshot.binary_file_count,
        python_file_count=snapshot.python_file_count,
        symbol_count=snapshot.symbol_count,
        indexer_version=snapshot.indexer_version,
        files=files,
    )


def list_repository_files(
    journal: object,
    repository_id: str,
    commit_sha: str,
    *,
    include_symbols: bool = False,
) -> tuple[RepositoryFile, ...]:
    journal._ensure_open()
    commit_sha = _validate_sha(commit_sha.lower(), field="commit_sha")
    try:
        rows = journal._connection.execute(_FILE_SELECT, (repository_id, commit_sha)).fetchall()
        symbols_by_path: dict[str, list[RepositorySymbol]] = {}
        if include_symbols:
            for symbol_row in journal._connection.execute(_SYMBOL_SELECT, (repository_id, commit_sha)).fetchall():
                path, symbol = _symbol_row(symbol_row)
                symbols_by_path.setdefault(path, []).append(symbol)
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="repository files read") from exc
    files: list[RepositoryFile] = []
    for row in rows:
        path = str(row[0])
        symbols = tuple(symbols_by_path.get(path, ())) if include_symbols else ()
        files.append(_file_row(row, symbols=symbols))
    return tuple(files)


def get_repository_file(
    journal: object,
    repository_id: str,
    commit_sha: str,
    path: str,
    *,
    include_symbols: bool = True,
) -> RepositoryFile | None:
    journal._ensure_open()
    path = validate_repo_relative_path(path)
    commit_sha = _validate_sha(commit_sha.lower(), field="commit_sha")
    try:
        row = journal._connection.execute(_FILE_ONE_SELECT, (repository_id, commit_sha, path)).fetchone()
        if row is None:
            return None
        symbols: tuple[RepositorySymbol, ...] = ()
        if include_symbols:
            symbol_rows = journal._connection.execute(
                _SYMBOLS_FOR_PATH, (repository_id, commit_sha, path)
            ).fetchall()
            symbols = tuple(_symbol_row(item)[1] for item in symbol_rows)
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="repository file read") from exc
    return _file_row(row, symbols=symbols)


def _identity_matches(existing: RepositorySnapshot, candidate: RepositorySnapshot) -> bool:
    return (
        existing.repository_id == candidate.repository_id
        and existing.commit_sha == candidate.commit_sha
        and existing.tree_sha == candidate.tree_sha
        and existing.file_count == candidate.file_count
        and existing.text_file_count == candidate.text_file_count
        and existing.binary_file_count == candidate.binary_file_count
        and existing.python_file_count == candidate.python_file_count
        and existing.symbol_count == candidate.symbol_count
        and existing.indexer_version == candidate.indexer_version
    )


def _files_match(existing_files: tuple[RepositoryFile, ...], candidate_files: tuple[RepositoryFile, ...]) -> bool:
    if len(existing_files) != len(candidate_files):
        return False
    for left, right in zip(existing_files, candidate_files, strict=True):
        if (
            left.path != right.path
            or left.git_mode != right.git_mode
            or left.git_object_type != right.git_object_type
            or left.object_sha != right.object_sha
            or left.size_bytes != right.size_bytes
            or left.content_sha256 != right.content_sha256
            or left.file_kind != right.file_kind
            or left.language != right.language
            or left.is_text != right.is_text
            or left.line_count != right.line_count
            or left.parse_status != right.parse_status
            or left.parse_diagnostic != right.parse_diagnostic
            or len(left.symbols) != len(right.symbols)
        ):
            return False
        for ls, rs in zip(left.symbols, right.symbols, strict=True):
            if (
                ls.symbol_id != rs.symbol_id
                or ls.parent_symbol_id != rs.parent_symbol_id
                or ls.kind != rs.kind
                or ls.name != rs.name
                or ls.qualified_name != rs.qualified_name
                or ls.start_line != rs.start_line
                or ls.end_line != rs.end_line
                or ls.start_column != rs.start_column
                or ls.end_column != rs.end_column
                or ls.signature != rs.signature
                or ls.decorators != rs.decorators
                or ls.docstring_summary != rs.docstring_summary
                or ls.ordinal != rs.ordinal
            ):
                return False
    return True


def save_repository_snapshot(journal: object, snapshot: RepositorySnapshot) -> IndexPersistOutcome:
    journal._ensure_open()
    if not isinstance(snapshot.repository_id, str) or not snapshot.repository_id:
        raise BridgeError("invalid_payload", "repository_id must be a non-empty string")
    commit_sha = _validate_sha(snapshot.commit_sha.lower(), field="commit_sha")
    tree_sha = _validate_sha(snapshot.tree_sha.lower(), field="tree_sha")
    parse_strict_utc_timestamp(snapshot.indexed_at, field="indexed_at")
    if snapshot.file_count != len(snapshot.files):
        raise BridgeError("invalid_payload", "file_count does not match files")
    if snapshot.symbol_count != sum(len(item.symbols) for item in snapshot.files):
        raise BridgeError("invalid_payload", "symbol_count does not match symbols")

    existing = get_repository_snapshot(
        journal,
        snapshot.repository_id,
        commit_sha,
        include_files=True,
        include_symbols=True,
    )
    if existing is not None:
        if _identity_matches(existing, snapshot) and _files_match(existing.files, snapshot.files):
            return IndexPersistOutcome(snapshot=existing, created=False, idempotent=True)
        raise BridgeError(
            "journal_conflict",
            "Existing repository snapshot conflicts with recomputed immutable index",
        )

    try:
        with journal._transaction():
            journal._connection.execute(
                """INSERT INTO repository_snapshots(
                    repository_id, commit_sha, tree_sha, indexed_at, file_count, text_file_count,
                    binary_file_count, python_file_count, symbol_count, indexer_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.repository_id,
                    commit_sha,
                    tree_sha,
                    snapshot.indexed_at,
                    snapshot.file_count,
                    snapshot.text_file_count,
                    snapshot.binary_file_count,
                    snapshot.python_file_count,
                    snapshot.symbol_count,
                    snapshot.indexer_version,
                ),
            )
            for file_record in snapshot.files:
                path = validate_repo_relative_path(file_record.path)
                diagnostic = None
                if file_record.parse_diagnostic is not None:
                    diagnostic = sanitize_diagnostics(file_record.parse_diagnostic, limit=500) or None
                journal._connection.execute(
                    """INSERT INTO repository_files(
                        repository_id, commit_sha, path, git_mode, git_object_type, object_sha,
                        size_bytes, content_sha256, file_kind, language, is_text, line_count,
                        parse_status, parse_diagnostic
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot.repository_id,
                        commit_sha,
                        path,
                        file_record.git_mode,
                        file_record.git_object_type,
                        _validate_sha(file_record.object_sha.lower(), field="object_sha"),
                        file_record.size_bytes,
                        file_record.content_sha256,
                        file_record.file_kind.value,
                        file_record.language,
                        1 if file_record.is_text else 0,
                        file_record.line_count,
                        file_record.parse_status.value,
                        diagnostic,
                    ),
                )
                for symbol in file_record.symbols:
                    journal._connection.execute(
                        """INSERT INTO repository_symbols(
                            repository_id, commit_sha, path, symbol_id, parent_symbol_id, kind, name,
                            qualified_name, start_line, end_line, start_column, end_column, signature,
                            decorators_json, docstring_summary, ordinal
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            snapshot.repository_id,
                            commit_sha,
                            path,
                            symbol.symbol_id,
                            symbol.parent_symbol_id,
                            symbol.kind.value,
                            symbol.name,
                            symbol.qualified_name,
                            symbol.start_line,
                            symbol.end_line,
                            symbol.start_column,
                            symbol.end_column,
                            symbol.signature,
                            decorators_json(symbol.decorators),
                            symbol.docstring_summary,
                            symbol.ordinal,
                        ),
                    )
    except BridgeError:
        raise
    except sqlite3.Error as exc:
        raise map_sqlite_error(exc, context="repository snapshot persist") from exc

    stored = get_repository_snapshot(
        journal,
        snapshot.repository_id,
        commit_sha,
        include_files=True,
        include_symbols=True,
    )
    if stored is None:
        raise BridgeError("journal_corrupt", "Repository snapshot missing after persist")
    return IndexPersistOutcome(snapshot=stored, created=True, idempotent=False)


def install_journal_repository_index_api(journal_cls: Type[object]) -> None:
    for name, fn in {
        "get_repository_snapshot": get_repository_snapshot,
        "list_repository_files": list_repository_files,
        "get_repository_file": get_repository_file,
        "save_repository_snapshot": save_repository_snapshot,
    }.items():
        setattr(journal_cls, name, fn)
