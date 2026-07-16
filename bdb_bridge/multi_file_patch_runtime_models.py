from __future__ import annotations

from dataclasses import dataclass

from .models import ExecutionOutcome, ProfileRunOutcome
from .multi_file_patch_recovery_models import MultiFileCheckpointBundle


@dataclass(frozen=True)
class MultiFilePatchProfileRecord:
    command_id: str
    profile_id: str
    status: str
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    stdout_sha256: str
    stderr_sha256: str
    duration_ms: int
    started_at: str
    finished_at: str
    created_at: str

    def to_outcome(self) -> ProfileRunOutcome:
        return ProfileRunOutcome(
            status=self.status,
            exit_code=self.exit_code,
            stdout=self.stdout_tail,
            stderr=self.stderr_tail,
            duration_ms=self.duration_ms,
        )


@dataclass(frozen=True)
class MultiFilePatchRuntimeResult:
    checkpoint: MultiFileCheckpointBundle
    profile: MultiFilePatchProfileRecord
    outcome: ExecutionOutcome
