from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.git_object_reader import GitObjectReader
from bdb_bridge.python_symbol_parser import parse_python_symbols
from bdb_bridge.repository_index_builder import RepositoryIndexBuilder
from bdb_bridge.repository_index_models import FileKind, ParseStatus, SymbolKind
from bdb_bridge.repository_index_service import RepositoryIndexService
from tests.helpers.repository_index_fixture import NOW, REPO_ID, git, make_index_fixture, write_blob


def test_index_exact_commit_ignores_working_tree_changes(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    service = RepositoryIndexService(cfg, journal)
    service.index(commits["commit1"])
    write_blob(fixture, "src/sample.py", b"def mutated():\n    return 1\n")
    write_blob(fixture, "untracked.txt", b"nope\n")
    git(fixture, "add", "src/sample.py")
    second = service.index(commits["commit1"])
    assert second.idempotent is True
    assert second.snapshot.commit_sha == commits["commit1"]
    files = {item.path: item for item in second.snapshot.files}
    blob = git(fixture, "cat-file", "blob", f"{commits['commit1']}:src/sample.py")
    assert files["src/sample.py"].content_sha256 == hashlib.sha256(blob).hexdigest()
    assert "untracked.txt" not in files
    with pytest.raises(BridgeError) as exc:
        GitObjectReader(fixture).resolve_commit("--help")
    assert exc.value.code == "invalid_payload"
    journal.close()


def test_two_commits_coexist_and_reindex_is_idempotent(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    service = RepositoryIndexService(cfg, journal)
    one = service.index(commits["commit1"])
    three = service.index(commits["commit3"])
    again = service.index(commits["commit1"])
    assert one.created is True
    assert three.created is True
    assert again.idempotent is True
    assert one.snapshot.commit_sha != three.snapshot.commit_sha
    assert journal.get_repository_snapshot(REPO_ID, commits["commit1"]) is not None
    assert journal.get_repository_snapshot(REPO_ID, commits["commit3"]) is not None
    count = journal._connection.execute("SELECT COUNT(*) FROM repository_snapshots").fetchone()[0]
    assert count == 2
    journal.close()


def test_file_classification_matrix(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    outcome = RepositoryIndexService(cfg, journal).index(commits["commit3"])
    files = {item.path: item for item in outcome.snapshot.files}
    assert files["text/empty.txt"].is_text is True
    assert files["text/empty.txt"].line_count == 0
    assert files["bin/data.bin"].is_text is False
    assert files["bin/data.bin"].parse_status is ParseStatus.BINARY
    assert files["text/invalid.txt"].is_text is False
    assert files["src/too_large.py"].parse_status is ParseStatus.TOO_LARGE
    assert files["src/too_large.py"].symbols == ()
    assert files["docs/note.md"].parse_status is ParseStatus.UNSUPPORTED_LANGUAGE
    assert files["paths/file with space.txt"].is_text is True
    assert "paths/unicodę.txt" in files
    assert files["broken/syntax.py"].parse_status is ParseStatus.SYNTAX_ERROR
    assert files["vendor/nested"].file_kind is FileKind.SUBMODULE
    assert files["vendor/nested"].parse_status is ParseStatus.METADATA_ONLY
    assert files["vendor/nested"].is_text is False
    assert files["vendor/nested"].line_count is None
    if commits["has_symlink"]:
        assert files["links/note_link"].file_kind is FileKind.SYMLINK
        assert files["links/note_link"].parse_status is ParseStatus.METADATA_ONLY
    journal.close()


def test_python_symbols_cover_required_shapes(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    outcome = RepositoryIndexService(cfg, journal).index(commits["commit1"])
    sample = next(item for item in outcome.snapshot.files if item.path == "src/sample.py")
    by_qname = {item.qualified_name: item for item in sample.symbols}
    assert by_qname["module_function"].kind is SymbolKind.FUNCTION
    assert by_qname["module_async"].kind is SymbolKind.ASYNC_FUNCTION
    assert by_qname["Outer"].kind is SymbolKind.CLASS
    assert by_qname["Outer.method"].kind is SymbolKind.METHOD
    assert by_qname["Outer.async_method"].kind is SymbolKind.ASYNC_METHOD
    assert by_qname["Outer.guarded_method"].kind is SymbolKind.METHOD
    assert by_qname["module_function.nested"].kind is SymbolKind.NESTED_FUNCTION
    assert by_qname["module_function.nested_async"].kind is SymbolKind.NESTED_ASYNC_FUNCTION
    assert by_qname["guarded_function"].kind is SymbolKind.FUNCTION
    assert by_qname["Outer.Inner"].kind is SymbolKind.NESTED_CLASS
    assert by_qname["decorated"].decorators == ("decorator",)
    assert by_qname["module_function"].docstring_summary == "First summary line."
    assert "*args" in (by_qname["module_function"].signature or "")
    assert "**kwargs" in (by_qname["module_function"].signature or "")
    assert [item.ordinal for item in sample.symbols] == list(range(len(sample.symbols)))
    source = git(fixture, "cat-file", "blob", f"{commits['commit1']}:src/sample.py").decode("utf-8")
    again = parse_python_symbols(
        source=source,
        repository_id=REPO_ID,
        commit_sha=commits["commit1"],
        path="src/sample.py",
    )
    assert [item.symbol_id for item in again.symbols] == [item.symbol_id for item in sample.symbols]
    journal.close()


def test_syntax_error_does_not_abort_snapshot_and_side_effect_not_executed(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    outcome = RepositoryIndexService(cfg, journal).index(commits["commit1"])
    files = {item.path: item for item in outcome.snapshot.files}
    assert files["broken/syntax.py"].parse_status is ParseStatus.SYNTAX_ERROR
    assert files["broken/syntax.py"].symbols == ()
    assert files["src/side_effect.py"].parse_status is ParseStatus.OK
    assert files["src/side_effect.py"].symbols == ()
    journal.close()


def test_persist_atomicity_and_conflict(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_index_fixture(tmp_path)
    service = RepositoryIndexService(cfg, journal)
    outcome = service.index(commits["commit1"])
    stored = journal.get_repository_snapshot(REPO_ID, commits["commit1"], include_files=True, include_symbols=True)
    assert stored is not None
    corrupted = stored.__class__(
        repository_id=stored.repository_id,
        commit_sha=stored.commit_sha,
        tree_sha="c" * 40,
        indexed_at=stored.indexed_at,
        file_count=stored.file_count,
        text_file_count=stored.text_file_count,
        binary_file_count=stored.binary_file_count,
        python_file_count=stored.python_file_count,
        symbol_count=stored.symbol_count,
        indexer_version=stored.indexer_version,
        files=stored.files,
    )
    with pytest.raises(BridgeError) as exc:
        journal.save_repository_snapshot(corrupted)
    assert exc.value.code == "journal_conflict"

    builder = RepositoryIndexBuilder(
        repo_path=fixture,
        repository_id=REPO_ID,
        now_fn=lambda: NOW,
    )
    snapshot = builder.build(commits["commit2"])
    try:
        with journal._transaction():
            journal._connection.execute(
                """INSERT INTO repository_snapshots(
                    repository_id, commit_sha, tree_sha, indexed_at, file_count, text_file_count,
                    binary_file_count, python_file_count, symbol_count, indexer_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.repository_id,
                    snapshot.commit_sha,
                    snapshot.tree_sha,
                    snapshot.indexed_at,
                    snapshot.file_count,
                    snapshot.text_file_count,
                    snapshot.binary_file_count,
                    snapshot.python_file_count,
                    snapshot.symbol_count,
                    snapshot.indexer_version,
                ),
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert journal.get_repository_snapshot(REPO_ID, commits["commit2"]) is None
    journal.close()
    assert outcome.created is True
