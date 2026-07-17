from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from bdb_bridge.workspace_context import WorkspaceContextBuilder


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Workspace Context Test")
    git(root, "config", "user.email", "workspace-context@example.invalid")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "private").mkdir()
    (root / "src" / "app.py").write_text(
        "class Calculator:\n"
        "    def add(self, left: int, right: int) -> int:\n"
        "        return left + right\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "tests" / "test_app.py").write_text(
        "def test_placeholder() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "private" / "secret.txt").write_text("must not be disclosed\n", encoding="utf-8")
    (root / "src" / "binary.bin").write_bytes(b"\x00\xff\x01")
    git(root, "add", "--", ".")
    git(root, "commit", "-m", "fixture")
    return root


def test_snapshot_returns_only_allowed_text_files_and_symbols(tmp_path: Path) -> None:
    root = repository(tmp_path)
    config = SimpleNamespace(
        fixture_repo_path=root,
        allowed_paths=("src/*.py", "tests/*.py"),
    )

    snapshot = WorkspaceContextBuilder(config).build()

    assert snapshot["source_clean"] is True
    assert snapshot["tracked_paths"] == ["src/app.py", "tests/test_app.py"]
    assert [item["path"] for item in snapshot["snapshot_files"]] == [
        "src/app.py",
        "tests/test_app.py",
    ]
    assert any(item["text"].startswith("class Calculator") for item in snapshot["symbols"])
    assert any("def add" in item["text"] for item in snapshot["symbols"])
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "must not be disclosed" not in serialized
    assert str(root) not in serialized
    assert snapshot["capabilities"]["workspace_context"] is True
    assert snapshot["capabilities"]["promotion_receipts"] is True


def test_snapshot_reports_only_allowed_dirty_path_names(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "src" / "app.py").write_text("def changed() -> bool:\n    return True\n", encoding="utf-8")
    (root / "private" / "secret.txt").write_text("new private value\n", encoding="utf-8")
    config = SimpleNamespace(
        fixture_repo_path=root,
        allowed_paths=("src/*.py",),
    )

    snapshot = WorkspaceContextBuilder(config).build()

    assert snapshot["source_clean"] is False
    assert snapshot["source_changes"] == ["src/app.py"]
    assert snapshot["source_changes_outside_scope"] == 1
    assert snapshot["tracked_paths"] == ["src/app.py"]
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "new private value" not in serialized
    assert "private/secret.txt" not in serialized
