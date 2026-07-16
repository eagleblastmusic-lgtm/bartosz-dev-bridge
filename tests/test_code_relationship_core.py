from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import BridgeError
from bdb_bridge.code_relationship_models import EdgeKind, ReferenceKind, ResolutionStatus
from bdb_bridge.code_relationship_service import RepositoryRelationshipService
from bdb_bridge.repository_index_service import RepositoryIndexService
from tests.helpers.code_relationship_fixture import REPO_ID, make_relationship_fixture, write_text, git


def _indexed_analysis(tmp_path: Path):
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    RepositoryIndexService(cfg, journal).index(commits["commit1"])
    service = RepositoryRelationshipService(cfg, journal)
    outcome = service.analyze(commits["commit1"])
    return cfg, journal, fixture, commits, service, outcome


def test_analysis_resolves_core_shapes_without_source_execution(tmp_path: Path) -> None:
    cfg, journal, fixture, commits, service, outcome = _indexed_analysis(tmp_path)
    analysis = outcome.analysis
    assert outcome.created is True
    imports = {(item.source_path, item.module_name, item.imported_name): item for item in analysis.imports}
    assert imports[("pkg/service.py", "os", None)].resolution_status is ResolutionStatus.EXTERNAL
    assert imports[("pkg/service.py", "pkg.tools", None)].resolved_path == "pkg/tools.py"
    assert imports[("pkg/service.py", "pkg.tools", "helper")].resolved_symbol_id is not None
    calls = [item for item in analysis.references if item.reference_kind is ReferenceKind.CALL]
    by_expression = {}
    for item in calls:
        by_expression.setdefault(item.expression, []).append(item)
    assert any(item.resolution_status is ResolutionStatus.RESOLVED for item in by_expression["h"])
    assert any(item.resolution_status is ResolutionStatus.RESOLVED for item in by_expression["tools.helper"])
    assert any(item.resolution_status is ResolutionStatus.RESOLVED for item in by_expression["self.local"])
    assert any(item.resolution_status is ResolutionStatus.DYNAMIC for item in by_expression["helper"])
    assert any(item.resolution_status is ResolutionStatus.DYNAMIC for item in by_expression["obj.missing"])
    assert any(item.expression == "recursive" and item.resolution_status is ResolutionStatus.RESOLVED for item in calls)
    assert analysis.call_edge_count > 0
    assert all(item.resolution_status is ResolutionStatus.RESOLVED for item in analysis.edges)
    journal.close()


def test_analysis_is_immutable_idempotent_and_commit_scoped(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    index = RepositoryIndexService(cfg, journal)
    index.index(commits["commit1"])
    service = RepositoryRelationshipService(cfg, journal)
    first = service.analyze(commits["commit1"])
    write_text(fixture, "pkg/service.py", "def working_tree_only():\n    return 1\n")
    second = service.analyze(commits["commit1"])
    assert second.idempotent is True
    assert second.analysis == first.analysis
    git(fixture, "restore", "pkg/service.py")
    index.index(commits["commit2"])
    third = service.analyze(commits["commit2"])
    assert third.created is True
    assert third.analysis.commit_sha != first.analysis.commit_sha
    assert journal.get_repository_analysis(REPO_ID, commits["commit1"]) is not None
    assert journal.get_repository_analysis(REPO_ID, commits["commit2"]) is not None
    journal.close()


def test_search_ranking_validation_and_snapshot_only_mode(tmp_path: Path) -> None:
    cfg, journal, fixture, commits = make_relationship_fixture(tmp_path)
    RepositoryIndexService(cfg, journal).index(commits["commit1"])
    service = RepositoryRelationshipService(cfg, journal)
    exact = service.search(ref=commits["commit1"], query="Child", kind="symbol", limit=20)
    assert exact[0].name == "Child" and exact[0].rank == 1
    qualified = service.search(ref=commits["commit1"], query="Child.run", kind="symbol", limit=20)
    assert qualified[0].qualified_name == "Child.run" and qualified[0].rank == 1
    paths = service.search(ref=commits["commit1"], query="pkg/service.py", kind="file", limit=20)
    assert paths[0].path == "pkg/service.py" and paths[0].rank == 3
    with pytest.raises(BridgeError) as empty:
        service.search(ref=commits["commit1"], query="   ")
    assert empty.value.code == "invalid_query"
    with pytest.raises(BridgeError):
        service.search(ref=commits["commit1"], query="x" * 201)
    journal.close()


def test_references_callers_and_symbol_selectors(tmp_path: Path) -> None:
    cfg, journal, fixture, commits, service, outcome = _indexed_analysis(tmp_path)
    helper_path, helper = service.select_symbol(ref=commits["commit1"], path="pkg/tools.py", qualified_name="helper")
    assert helper_path == "pkg/tools.py"
    selected, callers = service.callers(ref=commits["commit1"], symbol_id=helper.symbol_id)
    assert selected[1].symbol_id == helper.symbol_id
    assert {item.expression for item in callers} >= {"h", "tools.helper"}
    assert all(item.resolution_status is ResolutionStatus.RESOLVED for item in callers)
    _, outgoing = service.references(ref=commits["commit1"], path="pkg/service.py", qualified_name="caller", direction="outgoing")
    assert any(item.reference_kind is ReferenceKind.CALL for item in outgoing)
    with pytest.raises(BridgeError) as missing:
        service.select_symbol(ref=commits["commit1"], symbol_id="f" * 64)
    assert missing.value.code == "symbol_not_found"
    journal.close()


def test_dependency_graph_is_bounded_cycle_safe_and_filtered(tmp_path: Path) -> None:
    cfg, journal, fixture, commits, service, outcome = _indexed_analysis(tmp_path)
    graph = service.dependencies(ref=commits["commit1"], path="pkg/cycle_a.py", direction="outgoing", depth=5, max_nodes=20, edge_kind="call")
    assert graph["cycle"] is True
    assert {item["path"] for item in graph["nodes"]} >= {"pkg/cycle_a.py", "pkg/cycle_b.py"}
    assert all(item["edge_kind"] == EdgeKind.CALL.value for item in graph["edges"])
    bounded = service.dependencies(ref=commits["commit1"], path="pkg/service.py", depth=5, max_nodes=1)
    assert bounded["truncated"] is True and len(bounded["nodes"]) == 1
    with pytest.raises(BridgeError):
        service.dependencies(ref=commits["commit1"], path="../escape.py")
    journal.close()
