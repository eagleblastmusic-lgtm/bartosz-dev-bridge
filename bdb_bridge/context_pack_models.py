from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


CONTEXT_PACK_VERSION = "ghb1c-v1"
MAX_CONTEXT_FILES = 50
MIN_CONTEXT_BYTES = 1024
MAX_CONTEXT_BYTES = 256 * 1024
MAX_CONTEXT_DEPTH = 3
MAX_EXCERPT_LINES = 200
MAX_CONTEXT_SOURCE_FILE_BYTES = 1 * 1024 * 1024

DEFAULT_GATE_MAX_FILES = 200_000
DEFAULT_GATE_MAX_SYMBOLS = 2_000_000
DEFAULT_GATE_MAX_RELATIONSHIPS = 5_000_000
DEFAULT_GATE_SAMPLE_FILES = 20
DEFAULT_GATE_SAMPLE_BYTES = 32 * 1024


class ContextDirection(str, Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    BOTH = "both"


@dataclass(frozen=True)
class ContextExcerpt:
    start_line: int
    end_line: int
    reason: str
    text: str


@dataclass(frozen=True)
class ContextFile:
    path: str
    language: str
    content_sha256: str
    size_bytes: int
    selection_reason: str
    priority: int
    omitted_reason: str | None
    excerpts: tuple[ContextExcerpt, ...]


@dataclass(frozen=True)
class ContextPack:
    pack_version: str
    repository_id: str
    commit_sha: str
    seed_kind: str
    seed_value: str
    direction: ContextDirection
    depth: int
    max_files: int
    max_bytes: int
    max_excerpt_lines: int
    candidate_count: int
    selected_file_count: int
    excerpt_count: int
    source_bytes: int
    truncated: bool
    files: tuple[ContextFile, ...]
    pack_sha256: str


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class LargeRepositoryGateResult:
    gate_version: str
    repository_id: str
    commit_sha: str
    passed: bool
    metrics: dict[str, int | float | str | bool]
    checks: tuple[GateCheck, ...]
    sample_pack_sha256: str | None
