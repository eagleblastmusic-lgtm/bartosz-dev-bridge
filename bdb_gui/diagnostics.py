from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from bdb_operator import OperatorApi, OperatorResponse


GUI_DIAGNOSTICS_SCHEMA = "bdb-gui-diagnostics-v1"
GUI_DIAGNOSTICS_SECTION_SCHEMA = "bdb-gui-diagnostics-section-v1"
GUI_DIAGNOSTICS_EXPORT_SCHEMA = "bdb-gui-diagnostics-export-v1"
DIAGNOSTICS_ARCHIVE_SCHEMA = "bdb-diagnostics-archive-v1"
REDACTION_VERSION = "bdb-redaction-v1"
MAX_DIAGNOSTIC_LOG_LINES = 200
MAX_DIAGNOSTIC_LOG_BYTES = 262_144

_SECRET_KEY = re.compile(
    r"(?i)(token|password|passwd|secret|cookie|authorization|api[_-]?key|access[_-]?key|private[_-]?key)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|password|passwd|secret|cookie|authorization|api[_-]?key|access[_-]?key)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


class DiagnosticsOperator(Protocol):
    def capabilities(self) -> OperatorResponse:
        ...

    def status(self, workspace_root: str | Path) -> OperatorResponse:
        ...

    def current_operation(self, workspace_root: str | Path) -> OperatorResponse:
        ...

    def logs(
        self,
        workspace_root: str | Path,
        *,
        max_bytes: int = MAX_DIAGNOSTIC_LOG_BYTES,
        max_lines: int = MAX_DIAGNOSTIC_LOG_LINES,
    ) -> OperatorResponse:
        ...


@dataclass(frozen=True)
class DiagnosticsSection:
    name: str
    ok: bool
    operation_id: str
    project_alias: str | None
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    schema: str = GUI_DIAGNOSTICS_SECTION_SCHEMA

    @classmethod
    def from_response(cls, name: str, response: OperatorResponse) -> "DiagnosticsSection":
        if response.ok:
            return cls(
                name=name,
                ok=True,
                operation_id=response.operation_id,
                project_alias=response.project_alias,
                data=_sanitize_object(response.data),
            )
        return cls(
            name=name,
            ok=False,
            operation_id=response.operation_id,
            project_alias=response.project_alias,
            error_code=response.error.code if response.error is not None else "operator_error",
            error_message=(
                response.error.message if response.error is not None else f"{name} read failed"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "ok": self.ok,
            "operation_id": self.operation_id,
            "project_alias": self.project_alias,
            "data": _sanitize_object(self.data),
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }


@dataclass(frozen=True)
class DiagnosticsSnapshot:
    workspace_root: str
    generated_at: str
    sections: tuple[DiagnosticsSection, ...]
    versions: dict[str, str]
    read_only: bool = True
    mutation_operations_invoked: int = 0
    redaction_version: str = REDACTION_VERSION
    schema: str = GUI_DIAGNOSTICS_SCHEMA

    @property
    def complete(self) -> bool:
        return all(section.ok for section in self.sections)

    @property
    def project_alias(self) -> str | None:
        for section in self.sections:
            if section.project_alias:
                return section.project_alias
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workspace_root": self.workspace_root,
            "generated_at": self.generated_at,
            "complete": self.complete,
            "project_alias": self.project_alias,
            "read_only": self.read_only,
            "mutation_operations_invoked": self.mutation_operations_invoked,
            "redaction_version": self.redaction_version,
            "versions": dict(self.versions),
            "sections": [section.to_dict() for section in self.sections],
        }


@dataclass(frozen=True)
class DiagnosticsExportResult:
    output_path: str
    size_bytes: int
    sha256: str
    entries: tuple[str, ...]
    generated_at: str
    schema: str = GUI_DIAGNOSTICS_EXPORT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "output_path": self.output_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "entries": list(self.entries),
            "generated_at": self.generated_at,
        }


class DiagnosticsService:
    """Collects a bounded, sanitized, read-only snapshot through Operator API."""

    def __init__(self, operator: DiagnosticsOperator | None = None) -> None:
        self._operator = operator or OperatorApi()

    def collect(self, workspace_root: str | Path) -> DiagnosticsSnapshot:
        root = str(Path(workspace_root).expanduser().resolve(strict=False))
        responses = (
            ("capabilities", self._operator.capabilities()),
            ("status", self._operator.status(root)),
            ("current_operation", self._operator.current_operation(root)),
            (
                "logs",
                self._operator.logs(
                    root,
                    max_bytes=MAX_DIAGNOSTIC_LOG_BYTES,
                    max_lines=MAX_DIAGNOSTIC_LOG_LINES,
                ),
            ),
        )
        return DiagnosticsSnapshot(
            workspace_root=root,
            generated_at=_utc_now(),
            sections=tuple(DiagnosticsSection.from_response(name, response) for name, response in responses),
            versions=_versions(),
        )


class DiagnosticsExporter:
    """Writes one explicit sanitized ZIP without Journal DB or repository files."""

    def export(
        self,
        snapshot: DiagnosticsSnapshot,
        output_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> DiagnosticsExportResult:
        target = Path(output_path).expanduser().resolve(strict=False)
        if target.suffix.lower() != ".zip":
            raise ValueError("Diagnostics export path must end with .zip")
        if not target.parent.is_dir():
            raise ValueError("Diagnostics export parent directory does not exist")
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))
        if target.is_dir():
            raise ValueError("Diagnostics export path points to a directory")

        diagnostics_bytes = _json_bytes(snapshot.to_dict())
        sections: dict[str, bytes] = {
            f"sections/{section.name}.json": _json_bytes(section.to_dict())
            for section in snapshot.sections
        }
        entries: dict[str, bytes] = {"diagnostics.json": diagnostics_bytes, **sections}
        manifest = {
            "schema": DIAGNOSTICS_ARCHIVE_SCHEMA,
            "generated_at": _utc_now(),
            "snapshot_schema": snapshot.schema,
            "redaction_version": snapshot.redaction_version,
            "contains_journal_database": False,
            "contains_repository_files": False,
            "entries": {
                name: {"size_bytes": len(content), "sha256": _sha256_bytes(content)}
                for name, content in sorted(entries.items())
            },
        }
        entries["manifest.json"] = _json_bytes(manifest)

        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                for name, content in sorted(entries.items()):
                    info = zipfile.ZipInfo(name)
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    archive.writestr(info, content)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

        return DiagnosticsExportResult(
            output_path=str(target),
            size_bytes=target.stat().st_size,
            sha256=_sha256_path(target),
            entries=tuple(sorted(entries)),
            generated_at=_utc_now(),
        )


def _versions() -> dict[str, str]:
    try:
        package_version = metadata.version("bartosz-dev-bridge")
    except metadata.PackageNotFoundError:
        package_version = "source-tree"
    try:
        pyside_version = metadata.version("PySide6-Essentials")
    except metadata.PackageNotFoundError:
        pyside_version = "not-installed"
    return {
        "bartosz_dev_bridge": package_version,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "pyside6_essentials": pyside_version,
        "executable_name": Path(sys.executable).name,
    }


def _sanitize_object(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize_object(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_object(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_object(item) for item in value]
    if isinstance(value, str):
        sanitized = _BEARER.sub("Bearer [REDACTED]", value)
        return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", sanitized)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
