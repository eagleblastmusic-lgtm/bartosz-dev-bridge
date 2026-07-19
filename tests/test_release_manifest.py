from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdb_release import (
    RELEASE_MANIFEST_SCHEMA,
    ReleaseManifest,
    create_release_manifest,
    load_release_manifest,
    verify_release_artifact,
    write_release_manifest,
)


COMMIT = "a" * 40
BUILT_AT = "2026-07-19T00:00:00Z"


def artifact(tmp_path: Path, content: bytes = b"synthetic release payload") -> Path:
    path = tmp_path / "BDB-Control-Center-windows-x86_64-0.2.0.zip"
    path.write_bytes(content)
    return path


def test_create_write_load_and_verify_release_manifest(tmp_path: Path) -> None:
    package = artifact(tmp_path)
    manifest = create_release_manifest(
        package,
        version="0.2.0",
        source_commit=COMMIT,
        built_at=BUILT_AT,
    )
    manifest_path = write_release_manifest(manifest, tmp_path / "manifest.json")
    loaded = load_release_manifest(manifest_path)
    receipt = verify_release_artifact(loaded, package)

    assert loaded.schema == RELEASE_MANIFEST_SCHEMA
    assert loaded.version == "0.2.0"
    assert loaded.source_commit == COMMIT
    assert loaded.channel == "manual-artifact"
    assert loaded.auto_download is False
    assert loaded.auto_install is False
    assert loaded.published_release is False
    assert loaded.signature is None
    assert receipt.verified is True
    assert receipt.sha256 == loaded.artifact_sha256
    assert receipt.size_bytes == package.stat().st_size


def test_tampered_artifact_is_rejected(tmp_path: Path) -> None:
    package = artifact(tmp_path)
    manifest = create_release_manifest(
        package,
        version="0.2.0",
        source_commit=COMMIT,
        built_at=BUILT_AT,
    )
    package.write_bytes(b"tampered payload")

    with pytest.raises(ValueError, match="size|SHA-256"):
        verify_release_artifact(manifest, package)


def test_filename_mismatch_is_rejected(tmp_path: Path) -> None:
    package = artifact(tmp_path)
    manifest = create_release_manifest(
        package,
        version="0.2.0",
        source_commit=COMMIT,
        built_at=BUILT_AT,
    )
    renamed = tmp_path / "other.zip"
    renamed.write_bytes(package.read_bytes())

    with pytest.raises(ValueError, match="filename"):
        verify_release_artifact(manifest, renamed)


@pytest.mark.parametrize("version", ["", "0.2", "v0.2.0", "0.2.0 unsafe"])
def test_invalid_version_is_rejected(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        create_release_manifest(
            artifact(tmp_path),
            version=version,
            source_commit=COMMIT,
            built_at=BUILT_AT,
        )


def test_invalid_commit_and_automatic_distribution_are_rejected(tmp_path: Path) -> None:
    package = artifact(tmp_path)
    with pytest.raises(ValueError, match="source_commit"):
        create_release_manifest(
            package,
            version="0.2.0",
            source_commit="short",
            built_at=BUILT_AT,
        )

    valid = create_release_manifest(
        package,
        version="0.2.0",
        source_commit=COMMIT,
        built_at=BUILT_AT,
    )
    unsafe = ReleaseManifest(
        version=valid.version,
        source_commit=valid.source_commit,
        artifact_name=valid.artifact_name,
        artifact_size_bytes=valid.artifact_size_bytes,
        artifact_sha256=valid.artifact_sha256,
        built_at=valid.built_at,
        python_version=valid.python_version,
        auto_install=True,
    )
    with pytest.raises(ValueError, match="automatic"):
        unsafe.validate()


def test_unknown_manifest_fields_and_oversized_manifest_are_rejected(tmp_path: Path) -> None:
    package = artifact(tmp_path)
    manifest = create_release_manifest(
        package,
        version="0.2.0",
        source_commit=COMMIT,
        built_at=BUILT_AT,
    )
    document = manifest.to_dict()
    document["unexpected"] = True
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        load_release_manifest(invalid)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (128 * 1024 + 1) + b"}")
    with pytest.raises(ValueError, match="large"):
        load_release_manifest(oversized)


def test_manifest_write_requires_existing_json_parent(tmp_path: Path) -> None:
    manifest = create_release_manifest(
        artifact(tmp_path),
        version="0.2.0",
        source_commit=COMMIT,
        built_at=BUILT_AT,
    )
    with pytest.raises(ValueError, match="json"):
        write_release_manifest(manifest, tmp_path / "manifest.txt")
    with pytest.raises(ValueError, match="parent"):
        write_release_manifest(manifest, tmp_path / "missing" / "manifest.json")
