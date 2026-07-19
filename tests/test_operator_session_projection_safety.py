from __future__ import annotations

import json
from pathlib import Path

from bdb_operator import OperatorApi
from bdb_operator.session_projection import SessionProjectionReader

from .session_projection_fixture import SUCCESS_SESSION, workspace_fixture


def test_invalid_receipt_is_reported_without_hiding_the_session(tmp_path: Path) -> None:
    root, _, _, promotions = workspace_fixture(tmp_path)
    path = promotions / f"{SUCCESS_SESSION}-000001.json"
    path.write_text('{"schema":"wrong"}', encoding="utf-8")

    value = SessionProjectionReader.from_workspace_root(root).list_sessions(limit=10)
    attempt = value["sessions"][0]["attempts"][0]

    assert attempt["receipt"] is None
    assert attempt["receipt_file"]["exists"] is True
    assert attempt["receipt_file"]["valid"] is False
    assert "schema" in attempt["receipt_file"]["warning"].lower()


def test_result_directory_outside_runtime_is_rejected_before_journal_read(tmp_path: Path) -> None:
    root, journal, _, _ = workspace_fixture(tmp_path)
    config_path = root / "bridge-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["direct_result_dir"] = str(tmp_path / "outside")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    before = journal.read_bytes()

    response = OperatorApi(repo_root=tmp_path, platform_name="posix").sessions(root)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "observability_config_invalid"
    assert journal.read_bytes() == before


def test_session_limit_is_rejected_before_io(tmp_path: Path) -> None:
    root, journal, _, _ = workspace_fixture(tmp_path)
    before = journal.read_bytes()

    response = OperatorApi(repo_root=tmp_path, platform_name="posix").sessions(root, limit=101)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "invalid_argument"
    assert journal.read_bytes() == before
