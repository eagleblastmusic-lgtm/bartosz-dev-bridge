from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from bdb_gui.diagnostics import (
    DiagnosticsExporter,
    DiagnosticsSection,
    DiagnosticsSnapshot,
)


def diagnostic_snapshot(tmp_path: Path) -> DiagnosticsSnapshot:
    return DiagnosticsSnapshot(
        workspace_root=str(tmp_path / "alpha"),
        generated_at="2026-07-18T22:00:00Z",
        versions={"python": "3.12.0", "bartosz_dev_bridge": "0.2.0"},
        sections=(
            DiagnosticsSection(
                name="capabilities",
                ok=True,
                operation_id="cap-op",
                project_alias="alpha",
                data={"transport": "in_process", "token": "must-not-leak"},
            ),
            DiagnosticsSection(
                name="logs",
                ok=True,
                operation_id="logs-op",
                project_alias="alpha",
                data={
                    "sources": [
                        {
                            "source": "promoter_stdout",
                            "lines": ["password=secret-value", "Bearer dangerous-token"],
                        }
                    ]
                },
            ),
        ),
    )


def test_export_creates_atomic_sanitized_zip_with_manifest(tmp_path: Path) -> None:
    target = tmp_path / "diagnostics.zip"

    result = DiagnosticsExporter().export(diagnostic_snapshot(tmp_path), target)

    assert target.is_file()
    assert result.output_path == str(target.resolve())
    assert result.size_bytes == target.stat().st_size
    assert result.sha256 == "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    assert set(result.entries) == {
        "diagnostics.json",
        "manifest.json",
        "sections/capabilities.json",
        "sections/logs.json",
    }
    assert not list(tmp_path.glob(".diagnostics.zip.*.tmp"))

    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == set(result.entries)
        diagnostics = json.loads(archive.read("diagnostics.json"))
        manifest = json.loads(archive.read("manifest.json"))
        combined = b"\n".join(archive.read(name) for name in archive.namelist())

    assert diagnostics["read_only"] is True
    assert diagnostics["mutation_operations_invoked"] == 0
    assert manifest["schema"] == "bdb-diagnostics-archive-v1"
    assert manifest["contains_journal_database"] is False
    assert manifest["contains_repository_files"] is False
    assert "diagnostics.json" in manifest["entries"]
    assert b"must-not-leak" not in combined
    assert b"secret-value" not in combined
    assert b"dangerous-token" not in combined
    assert b"[REDACTED]" in combined
    assert not any(name.endswith((".sqlite", ".db")) for name in result.entries)


def test_existing_file_is_not_overwritten_without_explicit_flag(tmp_path: Path) -> None:
    target = tmp_path / "diagnostics.zip"
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        DiagnosticsExporter().export(diagnostic_snapshot(tmp_path), target)

    assert target.read_bytes() == b"existing"


def test_explicit_overwrite_replaces_same_target(tmp_path: Path) -> None:
    target = tmp_path / "diagnostics.zip"
    target.write_bytes(b"existing")

    result = DiagnosticsExporter().export(
        diagnostic_snapshot(tmp_path),
        target,
        overwrite=True,
    )

    assert result.size_bytes > len(b"existing")
    assert target.read_bytes() != b"existing"
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None


@pytest.mark.parametrize(
    "target",
    [
        "diagnostics.json",
        "diagnostics",
        "diagnostics.tar.gz",
    ],
)
def test_export_requires_zip_suffix(tmp_path: Path, target: str) -> None:
    with pytest.raises(ValueError, match="end with .zip"):
        DiagnosticsExporter().export(diagnostic_snapshot(tmp_path), tmp_path / target)


def test_export_parent_must_already_exist(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "diagnostics.zip"

    with pytest.raises(ValueError, match="parent directory"):
        DiagnosticsExporter().export(diagnostic_snapshot(tmp_path), target)

    assert not target.parent.exists()


def test_archive_entries_are_deterministically_ordered(tmp_path: Path) -> None:
    target_a = tmp_path / "a.zip"
    target_b = tmp_path / "b.zip"
    snapshot = diagnostic_snapshot(tmp_path)

    result_a = DiagnosticsExporter().export(snapshot, target_a)
    result_b = DiagnosticsExporter().export(snapshot, target_b)

    assert result_a.entries == tuple(sorted(result_a.entries))
    assert result_b.entries == result_a.entries
    with zipfile.ZipFile(target_a) as archive_a, zipfile.ZipFile(target_b) as archive_b:
        assert archive_a.namelist() == archive_b.namelist()
        for name in archive_a.namelist():
            if name != "manifest.json":
                assert archive_a.read(name) == archive_b.read(name)
