from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.code_relationship_service import RepositoryRelationshipService
from bdb_bridge.context_pack_service import (
    ContextPackService,
    _normalize_hints,
    context_pack_dict,
    render_context_markdown,
)
from bdb_bridge.repository_index_service import RepositoryIndexService
from tests.helpers.code_relationship_fixture import (
    git,
    make_relationship_fixture,
    write_text,
)


def _ready(tmp_path: Path):
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    RepositoryIndexService(cfg, journal).index(commits["commit1"])
    relationships = RepositoryRelationshipService(cfg, journal)
    relationships.analyze(commits["commit1"])
    return cfg, journal, fixture, commits, ContextPackService(cfg, journal), relationships


def test_context_pack_is_immutable_deterministic_and_bounded(tmp_path: Path) -> None:
    cfg, journal, fixture, commits, service, relationships = _ready(tmp_path)
    _, helper = relationships.select_symbol(
        ref=commits["commit1"], path="pkg/tools.py", qualified_name="helper"
    )
    first = service.build(
        ref=commits["commit1"],
        symbol_id=helper.symbol_id,
        direction="both",
        depth=2,
        max_files=8,
        max_bytes=4096,
        max_excerpt_lines=40,
    )
    write_text(fixture, "pkg/tools.py", "def working_tree_only():\n    return 99\n")
    second = service.build(
        ref=commits["commit1"],
        symbol_id=helper.symbol_id,
        direction="both",
        depth=2,
        max_files=8,
        max_bytes=4096,
        max_excerpt_lines=40,
    )
    assert first == second
    assert first.pack_sha256 == second.pack_sha256
    assert first.source_bytes <= 4096
    assert first.selected_file_count <= 8
    assert {item.path for item in first.files} >= {"pkg/tools.py", "pkg/service.py"}
    assert "working_tree_only" not in json.dumps(context_pack_dict(first), sort_keys=True)
    payload = context_pack_dict(first)
    digest_payload = dict(payload)
    digest_payload.pop("pack_sha256")
    expected = hashlib.sha256(
        json.dumps(
            digest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()
    assert first.pack_sha256 == expected
    markdown = render_context_markdown(first)
    assert markdown.startswith("# Repository context pack\n")
    assert str(tmp_path) not in markdown
    assert "1:" in markdown
    journal.close()


def test_context_pack_query_file_seed_and_validation(tmp_path: Path) -> None:
    cfg, journal, fixture, commits, service, relationships = _ready(tmp_path)
    query = service.build(
        ref=commits["commit1"], query="helper", depth=1, max_files=5, max_bytes=4096
    )
    assert query.seed_kind == "query"
    assert query.files[0].path == "pkg/tools.py"
    file_pack = service.build(
        ref=commits["commit1"], path="pkg/service.py", depth=0, max_files=1, max_bytes=4096
    )
    assert file_pack.seed_kind == "file"
    assert [item.path for item in file_pack.files] == ["pkg/service.py"]
    with pytest.raises(BridgeError) as multiple:
        service.build(ref=commits["commit1"], query="helper", path="pkg/tools.py")
    assert multiple.value.code == "invalid_payload"
    with pytest.raises(BridgeError):
        service.build(ref=commits["commit1"], path="../escape.py")
    with pytest.raises(BridgeError) as missing:
        service.build(ref=commits["commit1"], query="definitely-no-match")
    assert missing.value.code == "context_seed_not_found"
    with pytest.raises(BridgeError):
        service.build(ref=commits["commit1"], path="pkg/service.py", max_bytes=100)
    journal.close()


def test_context_hash_matches_cli_unicode_and_sensitive_paths_are_metadata_only(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    write_text(fixture, "pkg/unicode_mod.py", "def żółw():\n    return 'zażółć'\n")
    write_text(fixture, ".env.production", "SECRET_TOKEN=must-not-be-exported\n")
    git(fixture, "add", "-A")
    git(fixture, "commit", "-m", "add unicode and sensitive fixtures")
    commit = git(fixture, "rev-parse", "HEAD")
    RepositoryIndexService(cfg, journal).index(commit)
    RepositoryRelationshipService(cfg, journal).analyze(commit)
    service = ContextPackService(cfg, journal)
    unicode_pack = service.build(ref=commit, query="żółw", max_files=3, max_bytes=4096)
    payload = context_pack_dict(unicode_pack)
    digest_payload = dict(payload)
    digest_payload.pop("pack_sha256")
    expected = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert unicode_pack.pack_sha256 == expected
    sensitive = service.build(ref=commit, path=".env.production", depth=0, max_files=1, max_bytes=4096)
    assert sensitive.files[0].omitted_reason == "sensitive_path"
    assert sensitive.files[0].excerpts == ()
    assert "must-not-be-exported" not in json.dumps(context_pack_dict(sensitive), sort_keys=True)
    journal.close()

def test_gate_pins_exact_commit_and_excerpt_ranges_respect_line_cap(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    index = RepositoryIndexService(cfg, journal)
    relationships = RepositoryRelationshipService(cfg, journal)
    index.index(commits["commit1"])
    relationships.analyze(commits["commit1"])
    index.index(commits["commit2"])
    relationships.analyze(commits["commit2"])
    service = ContextPackService(cfg, journal)
    original = service.reader.resolve_commit
    moving_calls = 0

    def moving_ref(ref: str) -> str:
        nonlocal moving_calls
        if ref == "moving":
            moving_calls += 1
            return commits["commit1"] if moving_calls == 1 else commits["commit2"]
        return original(ref)

    service.reader.resolve_commit = moving_ref  # type: ignore[method-assign]
    result = service.gate(ref="moving", sample_max_files=5, sample_max_bytes=4096)
    assert result.commit_sha == commits["commit1"]
    assert moving_calls == 1
    ranges = _normalize_hints([(1, 10, "left", 1), (11, 20, "right", 2)], line_count=30, max_lines=10)
    assert [(start, end) for start, end, _reason, _priority in ranges] == [(1, 10), (11, 20)]
    assert all(end - start + 1 <= 10 for start, end, _reason, _priority in ranges)
    journal.close()

def test_large_repository_gate_reports_limits_and_bounded_sample(tmp_path: Path) -> None:
    cfg, journal, fixture, commits, service, relationships = _ready(tmp_path)
    passed = service.gate(
        ref=commits["commit1"], sample_max_files=5, sample_max_bytes=4096
    )
    assert passed.passed is True
    assert passed.sample_pack_sha256 is not None
    assert passed.metrics["sample_selected_files"] <= 5
    assert passed.metrics["sample_source_bytes"] <= 4096
    failed = service.gate(
        ref=commits["commit1"],
        max_files=1,
        sample_max_files=5,
        sample_max_bytes=4096,
    )
    assert failed.passed is False
    assert any(item.name == "file_limit" and not item.passed for item in failed.checks)
    journal.close()


def test_gate_remains_bounded_on_larger_synthetic_repository(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    for number in range(32):
        write_text(
            fixture,
            f"pkg/generated_{number:02d}.py",
            "from .tools import helper\n\n"
            f"def generated_{number:02d}():\n"
            "    return helper()\n",
        )
    git(fixture, "add", "-A")
    git(fixture, "commit", "-m", "add bounded large fixture")
    commit = git(fixture, "rev-parse", "HEAD")
    RepositoryIndexService(cfg, journal).index(commit)
    RepositoryRelationshipService(cfg, journal).analyze(commit)
    result = ContextPackService(cfg, journal).gate(
        ref=commit,
        max_files=100,
        max_symbols=1000,
        max_relationships=10000,
        sample_max_files=4,
        sample_max_bytes=4096,
    )
    assert result.passed is True
    assert result.metrics["file_count"] >= 38
    assert result.metrics["sample_selected_files"] <= 4
    assert result.metrics["sample_source_bytes"] <= 4096
    journal.close()
