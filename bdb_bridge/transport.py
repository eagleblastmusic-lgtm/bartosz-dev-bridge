from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RemoteDocument:
    path: str
    content: bytes
    document_commit_sha: str
    document_committed_at: str | None = None


@dataclass(frozen=True)
class CommandSnapshot:
    snapshot_sha: str
    manifests: tuple[RemoteDocument, ...]
    commands: tuple[RemoteDocument, ...]


class CommandTransport(Protocol):
    def fetch_snapshot(self) -> CommandSnapshot: ...
