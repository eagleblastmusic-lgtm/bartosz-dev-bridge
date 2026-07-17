from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .protocol import BridgeError, path_matches, validate_repo_relative_path
from .workspace_manager import Git, changed_paths


_MAX_TRACKED_PATHS = 2_000
_MAX_SNAPSHOT_FILES = 80
_MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_FILE_BYTES = 64 * 1024
_MAX_SYMBOLS = 500
_MAX_STATUS_PATHS = 200
_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_BASENAMES = frozenset(
    {
        ".editorconfig",
        ".gitignore",
        "Dockerfile",
        "Makefile",
        "Procfile",
    }
)
_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\s*\("),
    re.compile(r"^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\b"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_$][A-Za-z0-9_$]*\s*\("),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
    ),
    re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:class|interface|enum)\s+[A-Za-z_][A-Za-z0-9_]*\b"),
)


class WorkspaceContextBuilder:
    """Build a bounded, read-only snapshot without disclosing local absolute paths."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.root = Path(config.fixture_repo_path).expanduser().resolve(strict=True)
        self.git = Git(self.root)

    def build(self) -> dict[str, Any]:
        if not self.root.joinpath(".git").exists():
            raise BridgeError("invalid_fixture_repo", "Configured workspace is not a Git checkout")

        tracked, tracked_truncated = self._tracked_paths()
        status_text = self.git.run(["status", "--porcelain=v1"]).stdout
        source_changes = changed_paths(status_text)
        status_truncated = len(source_changes) > _MAX_STATUS_PATHS
        source_changes = source_changes[:_MAX_STATUS_PATHS]

        snapshots: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        snapshot_bytes = 0
        snapshot_truncated = False

        for relative in tracked:
            if len(symbols) >= _MAX_SYMBOLS and len(snapshots) >= _MAX_SNAPSHOT_FILES:
                snapshot_truncated = True
                break
            path = self._safe_path(relative)
            if path.is_symlink() or not path.is_file():
                skipped.append({"path": relative, "reason": "not_regular_file"})
                continue
            if not self._looks_textual(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                skipped.append({"path": relative, "reason": "stat_failed"})
                continue
            if size > _MAX_FILE_BYTES:
                skipped.append({"path": relative, "reason": "file_too_large"})
                continue
            try:
                data = path.read_bytes()
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                skipped.append({"path": relative, "reason": "not_utf8"})
                continue
            except OSError:
                skipped.append({"path": relative, "reason": "read_failed"})
                continue

            if len(symbols) < _MAX_SYMBOLS:
                symbols.extend(self._symbols(relative, text, _MAX_SYMBOLS - len(symbols)))

            if len(snapshots) >= _MAX_SNAPSHOT_FILES:
                snapshot_truncated = True
                continue
            if snapshot_bytes + len(data) > _MAX_SNAPSHOT_BYTES:
                snapshot_truncated = True
                continue
            snapshots.append(
                {
                    "path": relative,
                    "bytes": len(data),
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "content": text,
                }
            )
            snapshot_bytes += len(data)

        return {
            "source_clean": not status_text.strip(),
            "source_changes": source_changes,
            "source_changes_truncated": status_truncated,
            "tracked_paths": tracked,
            "tracked_paths_truncated": tracked_truncated,
            "snapshot_files": snapshots,
            "snapshot_bytes": snapshot_bytes,
            "snapshot_truncated": snapshot_truncated,
            "symbols": symbols[:_MAX_SYMBOLS],
            "symbols_truncated": len(symbols) >= _MAX_SYMBOLS,
            "skipped_files": skipped[:100],
            "capabilities": {
                "workspace_context": True,
                "open_read": True,
                "multi_file_patch": True,
                "automatic_continuation": True,
                "promotion_receipts": True,
            },
            "limits": {
                "tracked_paths": _MAX_TRACKED_PATHS,
                "snapshot_files": _MAX_SNAPSHOT_FILES,
                "snapshot_bytes": _MAX_SNAPSHOT_BYTES,
                "file_bytes": _MAX_FILE_BYTES,
                "symbols": _MAX_SYMBOLS,
            },
        }

    def _tracked_paths(self) -> tuple[list[str], bool]:
        raw = self.git.run(["ls-files", "-z"]).stdout
        values: list[str] = []
        for item in raw.split("\0"):
            if not item:
                continue
            normalized = validate_repo_relative_path(item.replace("\\", "/"))
            if path_matches(normalized, self.config.allowed_paths):
                values.append(normalized)
        values = sorted(set(values))
        truncated = len(values) > _MAX_TRACKED_PATHS
        return values[:_MAX_TRACKED_PATHS], truncated

    def _safe_path(self, relative: str) -> Path:
        normalized = validate_repo_relative_path(relative)
        if not path_matches(normalized, self.config.allowed_paths):
            raise BridgeError("policy_denied", f"Path is not allowed by local policy: {normalized}")
        candidate = self.root.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise BridgeError("unsafe_path", f"Workspace path escaped configured root: {normalized}") from exc
        return resolved

    @staticmethod
    def _looks_textual(path: Path) -> bool:
        return path.name in _TEXT_BASENAMES or path.suffix.lower() in _TEXT_SUFFIXES

    @staticmethod
    def _symbols(relative: str, text: str, remaining: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if remaining <= 0:
            return found
        for number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in _SYMBOL_PATTERNS):
                found.append({"path": relative, "line": number, "text": line.strip()[:300]})
                if len(found) >= remaining:
                    break
        return found
