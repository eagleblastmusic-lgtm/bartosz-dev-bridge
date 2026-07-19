from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


RELEASE_MANIFEST_SCHEMA = "bdb-release-manifest-v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactReceipt:
    path: str
    size_bytes: int
    sha256: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    source_commit: str
    artifact_name: str
    artifact_size_bytes: int
    artifact_sha256: str
    built_at: str
    python_version: str
    platform: str = "windows-x86_64"
    product: str = "BDB Control Center"
    channel: str = "manual-artifact"
    entrypoint: str = "BDB-Control-Center.exe"
    auto_download: bool = False
    auto_install: bool = False
    published_release: bool = False
    signature: None = None
    schema: str = RELEASE_MANIFEST_SCHEMA

    def validate(self) -> None:
        if self.schema != RELEASE_MANIFEST_SCHEMA:
            raise ValueError("Unsupported release manifest schema")
        if not _VERSION.fullmatch(self.version):
            raise ValueError("version must be semantic x.y.z")
        if not _COMMIT.fullmatch(self.source_commit):
            raise ValueError("source_commit must be a full lowercase Git SHA")
        if not self.artifact_name or Path(self.artifact_name).name != self.artifact_name:
            raise ValueError("artifact_name must be a basename")
        if not self.artifact_name.lower().endswith(".zip"):
            raise ValueError("artifact_name must end with .zip")
        if not 1 <= self.artifact_size_bytes <= _MAX_ARTIFACT_BYTES:
            raise ValueError("artifact_size_bytes is outside the supported range")
        if not _SHA256.fullmatch(self.artifact_sha256):
            raise ValueError("artifact_sha256 is invalid")
        if self.platform != "windows-x86_64":
            raise ValueError("only windows-x86_64 release artifacts are supported")
        if self.channel != "manual-artifact":
            raise ValueError("release channel must remain manual-artifact")
        if self.auto_download or self.auto_install or self.published_release:
            raise ValueError("automatic distribution and installation must remain disabled")
        if self.signature is not None:
            raise ValueError("v1 does not claim a signing mechanism")
        _parse_utc(self.built_at)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": self.schema,
            "product": self.product,
            "version": self.version,
            "channel": self.channel,
            "platform": self.platform,
            "source_commit": self.source_commit,
            "built_at": self.built_at,
            "python_version": self.python_version,
            "entrypoint": self.entrypoint,
            "artifact": {
                "name": self.artifact_name,
                "size_bytes": self.artifact_size_bytes,
                "sha256": self.artifact_sha256,
            },
            "distribution": {
                "auto_download": self.auto_download,
                "auto_install": self.auto_install,
                "published_release": self.published_release,
            },
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> "ReleaseManifest":
        if not isinstance(document, dict):
            raise ValueError("release manifest must be an object")
        expected = {
            "schema", "product", "version", "channel", "platform",
            "source_commit", "built_at", "python_version", "entrypoint",
            "artifact", "distribution", "signature",
        }
        if set(document) != expected:
            raise ValueError("release manifest fields do not match v1")
        artifact = document.get("artifact")
        distribution = document.get("distribution")
        if not isinstance(artifact, dict) or set(artifact) != {"name", "size_bytes", "sha256"}:
            raise ValueError("artifact receipt is invalid")
        if not isinstance(distribution, dict) or set(distribution) != {
            "auto_download", "auto_install", "published_release"
        }:
            raise ValueError("distribution policy is invalid")
        manifest = cls(
            schema=_string(document, "schema"),
            product=_string(document, "product"),
            version=_string(document, "version"),
            channel=_string(document, "channel"),
            platform=_string(document, "platform"),
            source_commit=_string(document, "source_commit"),
            built_at=_string(document, "built_at"),
            python_version=_string(document, "python_version"),
            entrypoint=_string(document, "entrypoint"),
            artifact_name=_string(artifact, "name"),
            artifact_size_bytes=_integer(artifact, "size_bytes"),
            artifact_sha256=_string(artifact, "sha256"),
            auto_download=_boolean(distribution, "auto_download"),
            auto_install=_boolean(distribution, "auto_install"),
            published_release=_boolean(distribution, "published_release"),
            signature=document.get("signature"),
        )
        manifest.validate()
        return manifest


def create_release_manifest(
    artifact_path: str | Path,
    *,
    version: str,
    source_commit: str,
    built_at: str | None = None,
) -> ReleaseManifest:
    artifact = Path(artifact_path).expanduser().resolve(strict=True)
    if not artifact.is_file():
        raise ValueError("artifact_path must be a file")
    size = artifact.stat().st_size
    manifest = ReleaseManifest(
        version=version,
        source_commit=source_commit,
        artifact_name=artifact.name,
        artifact_size_bytes=size,
        artifact_sha256=_sha256_path(artifact),
        built_at=built_at or _utc_now(),
        python_version=platform.python_version(),
    )
    manifest.validate()
    return manifest


def write_release_manifest(manifest: ReleaseManifest, output_path: str | Path) -> Path:
    target = Path(output_path).expanduser().resolve(strict=False)
    if target.suffix.lower() != ".json":
        raise ValueError("manifest output path must end with .json")
    if not target.parent.is_dir():
        raise ValueError("manifest output parent must exist")
    rendered = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def load_release_manifest(path: str | Path) -> ReleaseManifest:
    source = Path(path).expanduser().resolve(strict=True)
    if source.stat().st_size > 128 * 1024:
        raise ValueError("release manifest is unexpectedly large")
    try:
        document = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"release manifest is not valid JSON: {error}") from error
    return ReleaseManifest.from_dict(document)


def verify_release_artifact(
    manifest: ReleaseManifest | str | Path,
    artifact_path: str | Path,
) -> ArtifactReceipt:
    expected = load_release_manifest(manifest) if not isinstance(manifest, ReleaseManifest) else manifest
    expected.validate()
    artifact = Path(artifact_path).expanduser().resolve(strict=True)
    if artifact.name != expected.artifact_name:
        raise ValueError("artifact filename does not match manifest")
    if not artifact.is_file():
        raise ValueError("artifact_path must be a file")
    size = artifact.stat().st_size
    digest = _sha256_path(artifact)
    if size != expected.artifact_size_bytes:
        raise ValueError("artifact size does not match manifest")
    if digest != expected.artifact_sha256:
        raise ValueError("artifact SHA-256 does not match manifest")
    return ArtifactReceipt(path=str(artifact), size_bytes=size, sha256=digest, verified=True)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("built_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("built_at must include a timezone")


def _string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _integer(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _boolean(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value
