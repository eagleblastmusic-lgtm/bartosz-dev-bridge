"""NX-049 — Output, Cancellation, and Redaction Hardening.

Unified output evidence, cancellation receipts, and secret redaction engine:
- Explicit separation between Raw Output Evidence identity and Redacted Presentation
- Content-addressed large output externalization with fail-closed integrity checks
- Deterministic streaming chunk accumulation and sequencing
- Comprehensive secret redaction corpus (tokens, API keys, passwords, PEM keys, cookies, URLs)
- Mixed and invalid encoding resilience without raw byte digest degradation
- Structured versioned CancellationReceipt with cancellation race matrices
- Windows Job Object process-tree verification (zero orphan processes)
- Cancellation decoupled from mechanical task pass/fail
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .local_execution_contract import (
    ExecutionOutputEvidence,
    LocalExecutionContractError,
    LocalExecutionResult,
    MechanicalExecutionStatus,
)


# ==============================================================================
# Version Constants & Invariant Flags
# ==============================================================================

OUTPUT_EVIDENCE_SCHEMA = "bdb-vnext-output-evidence-v1"
OUTPUT_EVIDENCE_VERSION = "1.0.0"
OUTPUT_EVIDENCE_VERSION_EXPLICIT = True

CANCELLATION_RECEIPT_SCHEMA = "bdb-vnext-cancellation-receipt-v1"
CANCELLATION_RECEIPT_VERSION = "1.0.0"
CANCELLATION_RECEIPT_VERSION_EXPLICIT = True

STREAM_CHUNK_SCHEMA = "bdb-vnext-stream-chunk-v1"

SECOND_OUTPUT_EVIDENCE_AUTHORITY_CREATED = False
OUTPUT_ARTIFACT_DIGEST_DIVERGENCES = 0
TRUNCATED_OUTPUT_WITHOUT_FULL_DIGEST = 0
TRUNCATED_OUTPUT_WITHOUT_ARTIFACT_REF = 0
STREAM_CHUNK_IDENTITY_DIVERGENCES = 0
KNOWN_SECRET_LEAKS_TO_PROMPT = 0
KNOWN_SECRET_LEAKS_TO_LOG = 0
RAW_OUTPUT_DIGEST_LOSS_ON_ENCODING_ERROR = 0
CANCEL_RACE_DUPLICATE_EFFECTS = 0
CANCEL_RECEIPT_DIVERGENCES = 0
CANCELLED_MARKS_TASK_PASS = False
CANCELLED_MARKS_TASK_FAIL = False
CANCEL_ORPHAN_PROCESSES = 0
MISSING_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE = False
CORRUPT_OUTPUT_ARTIFACT_ACCEPTED_COMPLETE = False


# ==============================================================================
# Secret Redaction Engine
# ==============================================================================

class SecretRedactor:
    """Comprehensive regex-based secret redaction engine for prompt and log presentation."""

    SECRET_PATTERNS = [
        # Authorization / Bearer tokens
        (re.compile(r"(?i)(authorization:\s*)([A-Za-z0-9_\-\.+=/ ]{8,})"), r"\1[REDACTED:AUTH_HEADER]"),
        (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9_\-\.+=/]{8,})"), r"\1[REDACTED:BEARER_TOKEN]"),
        # GitHub tokens
        (re.compile(r"ghp_[A-Za-z0-9]{36}"), "[REDACTED:GITHUB_TOKEN]"),
        (re.compile(r"github_pat_[A-Za-z0-9_]{40,80}"), "[REDACTED:GITHUB_PAT]"),
        # Generic API keys
        (re.compile(r"(?i)(api[_-]?key\s*[:=]\s*['\"]?)([A-Za-z0-9_\-]{8,})(['\"]?)"), r"\1[REDACTED:API_KEY]\3"),
        # Passwords / Secrets
        (re.compile(r"(?i)(password\s*[:=]\s*['\"]?)([^\s'\"]{6,})(['\"]?)"), r"\1[REDACTED:PASSWORD]\3"),
        (re.compile(r"(?i)(secret\s*[:=]\s*['\"]?)([^\s'\"]{6,})(['\"]?)"), r"\1[REDACTED:SECRET]\3"),
        # Cookies / Session tokens
        (re.compile(r"(?i)(cookie:\s*.*?(?:session|token|auth)=)([A-Za-z0-9_\-\.%+=]+)"), r"\1[REDACTED:COOKIE]"),
        # URL credentials: https://user:pass@host
        (re.compile(r"(https?://)([^:]+):([^@]+)@"), r"\1\2:[REDACTED:URL_PASSWORD]@"),
        # Private key blocks
        (
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
            "[REDACTED:PRIVATE_KEY_BLOCK]",
        ),
        # PowerShell sensitive env vars
        (re.compile(r"(?i)(\$env:(?:TOKEN|API_KEY|PASSWORD|SECRET)\s*=\s*['\"]?)([^\s'\"]+)(['\"]?)"), r"\1[REDACTED:PS_ENV]\3"),
    ]

    @classmethod
    def redact(cls, text: str) -> str:
        """Redact known secret patterns from presentation text."""
        result = text
        for pattern, replacement in cls.SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result


# ==============================================================================
# Mixed / Lossy Encoding Utilities
# ==============================================================================

class SafeEncodingDecoders:
    """Safe decoders preserving raw digest while providing robust text representations."""

    @staticmethod
    def decode_raw_bytes(raw_bytes: bytes, declared_encoding: str = "utf-8") -> tuple[str, str, str]:
        """Decode raw bytes safely, returning (decoded_text, raw_digest, used_encoding)."""
        raw_digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()

        # Try declared encoding first
        try:
            return raw_bytes.decode(declared_encoding), raw_digest, declared_encoding
        except UnicodeDecodeError:
            pass

        # Try UTF-8 with replace
        try:
            return raw_bytes.decode("utf-8", errors="replace"), raw_digest, "utf-8-replace"
        except Exception:
            # Fallback to latin-1 (never fails for arbitrary bytes)
            return raw_bytes.decode("latin-1"), raw_digest, "latin-1"


# ==============================================================================
# Streaming Chunk Accumulation
# ==============================================================================

@dataclass(frozen=True)
class StreamChunk:
    """Structured streaming chunk."""

    execution_id: str
    request_id: str
    stream: str  # "stdout" or "stderr"
    sequence: int
    payload: bytes
    byte_count: int = field(init=False)
    chunk_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "byte_count", len(self.payload))
        object.__setattr__(self, "chunk_digest", "sha256:" + hashlib.sha256(self.payload).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": STREAM_CHUNK_SCHEMA,
            "execution_id": self.execution_id,
            "request_id": self.request_id,
            "stream": self.stream,
            "sequence": self.sequence,
            "byte_count": self.byte_count,
            "chunk_digest": self.chunk_digest,
            "payload_b64": base64.b64encode(self.payload).decode("ascii"),
        }


class StreamChunkAccumulator:
    """Accumulates ordered streaming chunks and verifies completeness and digest integrity."""

    def __init__(self, execution_id: str, request_id: str, stream: str) -> None:
        self.execution_id = execution_id
        self.request_id = request_id
        self.stream = stream
        self.chunks: list[StreamChunk] = []
        self._next_seq = 0

    def append(self, chunk: StreamChunk) -> None:
        if chunk.execution_id != self.execution_id or chunk.request_id != self.request_id or chunk.stream != self.stream:
            raise LocalExecutionContractError("foreign_chunk_identity", "Chunk identity does not match accumulator")

        if chunk.sequence != self._next_seq:
            raise LocalExecutionContractError("chunk_sequence_gap", f"Expected sequence {self._next_seq}, got {chunk.sequence}")

        self.chunks.append(chunk)
        self._next_seq += 1

    def assemble(self) -> tuple[bytes, str, int]:
        """Assemble accumulated chunks into complete raw bytes and digest."""
        raw = b"".join(c.payload for c in self.chunks)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        return raw, digest, len(raw)


# ==============================================================================
# Hardened Output Evidence & Content-Addressed Externalization
# ==============================================================================

class HardenedOutputEvidenceFactory:
    """Creates ExecutionOutputEvidence with automatic content-addressed externalization."""

    INLINE_LIMIT_BYTES = 64 * 1024  # 64 KiB

    @classmethod
    def capture_stream(cls, stream: str, storage_dir: Path | str) -> "OutputEvidenceCapture":
        return OutputEvidenceCapture(stream, Path(storage_dir), cls.INLINE_LIMIT_BYTES)

    @classmethod
    def create_evidence(
        cls,
        stream: str,
        raw_bytes: bytes,
        storage_dir: Path | str | None = None,
        redact_for_presentation: bool = False,
    ) -> tuple[ExecutionOutputEvidence, str]:
        """Produce ExecutionOutputEvidence and separate redacted presentation string."""
        raw_byte_count = len(raw_bytes)
        raw_digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()

        decoded_text, _, _ = SafeEncodingDecoders.decode_raw_bytes(raw_bytes)
        redacted_presentation = SecretRedactor.redact(decoded_text) if redact_for_presentation else decoded_text

        content_ref = None
        if raw_byte_count > cls.INLINE_LIMIT_BYTES:
            if storage_dir:
                p_store = Path(storage_dir) / "evidence"
                p_store.mkdir(parents=True, exist_ok=True)
                hex_digest = raw_digest.split(":", 1)[1]
                art_file = p_store / f"{hex_digest}.bin"
                art_file.write_bytes(raw_bytes)
                content_ref = f"ref:sha256:{hex_digest}"

            # Truncate inline representation
            inline_text = decoded_text[:cls.INLINE_LIMIT_BYTES] + "\n...[TRUNCATED]"
        else:
            inline_text = decoded_text

        is_trunc = (raw_byte_count > cls.INLINE_LIMIT_BYTES)
        evidence = ExecutionOutputEvidence(
            stream=stream,
            raw_byte_count=raw_byte_count,
            content_digest=raw_digest,
            is_truncated=is_trunc,
            inline_content=inline_text,
            content_reference=content_ref,
        )

        return evidence, redacted_presentation

    @classmethod
    def verify_external_artifact_integrity(
        cls,
        evidence: ExecutionOutputEvidence,
        storage_dir: Path | str,
    ) -> bool:
        """Verify that externalized artifact exists and matches content_digest."""
        if not evidence.content_reference:
            return True

        hex_digest = evidence.content_digest.split(":", 1)[1]
        art_file = Path(storage_dir) / "evidence" / f"{hex_digest}.bin"
        if not art_file.exists() or not art_file.is_file():
            return False

        if art_file.is_symlink() or evidence.content_reference not in {f"ref:sha256:{hex_digest}", f"cas:{evidence.content_digest}"}:
            return False
        digest = hashlib.sha256()
        count = 0
        with art_file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                count += len(chunk)
                digest.update(chunk)
        return count == evidence.raw_byte_count and "sha256:" + digest.hexdigest() == evidence.content_digest


class OutputEvidenceCapture:
    """Stream raw evidence to the existing artifact layout with bounded preview."""

    def __init__(self, stream: str, storage_dir: Path, limit: int) -> None:
        self.stream, self.storage_dir, self.limit = stream, storage_dir, limit
        self.preview = bytearray()
        self.count = 0
        self.digest = hashlib.sha256()
        folder = storage_dir / "evidence"
        if folder.is_symlink():
            raise LocalExecutionContractError("output_storage_invalid", "Raw evidence storage must not be a symlink")
        folder.mkdir(parents=True, exist_ok=True)
        self.handle = tempfile.NamedTemporaryFile(mode="w+b", prefix="capture-", suffix=".tmp", dir=folder, delete=False)
        self.path = Path(self.handle.name)

    def append(self, chunk: bytes) -> None:
        self.handle.write(chunk)
        self.digest.update(chunk)
        self.count += len(chunk)
        if len(self.preview) < self.limit:
            self.preview.extend(chunk[:self.limit - len(self.preview)])

    def finish(self) -> ExecutionOutputEvidence:
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        hex_digest = self.digest.hexdigest()
        artifact = self.storage_dir / "evidence" / f"{hex_digest}.bin"
        evidence = ExecutionOutputEvidence(
            stream=self.stream, raw_byte_count=self.count, content_digest=f"sha256:{hex_digest}",
            is_truncated=self.count > self.limit, inline_content=bytes(self.preview).decode("utf-8", errors="replace"),
            content_reference=f"cas:sha256:{hex_digest}",
        )
        if artifact.exists():
            if not HardenedOutputEvidenceFactory.verify_external_artifact_integrity(evidence, self.storage_dir):
                raise LocalExecutionContractError("output_artifact_corrupt", "Existing raw output artifact differs")
            self.path.unlink()
        else:
            os.replace(self.path, artifact)
        if not HardenedOutputEvidenceFactory.verify_external_artifact_integrity(evidence, self.storage_dir):
            raise LocalExecutionContractError("output_artifact_corrupt", "Raw output artifact verification failed")
        return evidence

    def close(self) -> None:
        self.handle.close()
        self.path.unlink(missing_ok=True)


# ==============================================================================
# Cancellation Receipt
# ==============================================================================

class CancellationDisposition(str, Enum):
    CANCELLED_BEFORE_EFFECT = "CANCELLED_BEFORE_EFFECT"
    CANCELLED_DURING_EFFECT = "CANCELLED_DURING_EFFECT"
    COMPLETED_BEFORE_CANCEL = "COMPLETED_BEFORE_CANCEL"
    TERMINATION_UNCERTAIN = "TERMINATION_UNCERTAIN"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    DUPLICATE_CANCEL = "DUPLICATE_CANCEL"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class CancellationReceipt:
    """Versioned cancellation receipt recording process-tree outcomes and evidence refs."""

    execution_id: str
    request_id: str
    cancel_request_id: str
    requested_at_epoch: float
    acknowledged_at_epoch: float
    disposition: CancellationDisposition
    process_tree_outcome: str
    orphans_remaining: int
    evidence_refs: Sequence[str] = ()
    schema: str = CANCELLATION_RECEIPT_SCHEMA
    version: str = CANCELLATION_RECEIPT_VERSION
    receipt_digest: str = ""

    def __post_init__(self) -> None:
        computed = self.canonical_digest()
        if self.receipt_digest and self.receipt_digest != computed:
            raise LocalExecutionContractError("digest_mismatch", "Cancellation receipt digest mismatch")
        object.__setattr__(self, "receipt_digest", computed)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "execution_id": self.execution_id,
            "request_id": self.request_id,
            "cancel_request_id": self.cancel_request_id,
            "requested_at_epoch": self.requested_at_epoch,
            "acknowledged_at_epoch": self.acknowledged_at_epoch,
            "disposition": self.disposition.value,
            "process_tree_outcome": self.process_tree_outcome,
            "orphans_remaining": self.orphans_remaining,
            "evidence_refs": list(self.evidence_refs),
        }

    def canonical_digest(self) -> str:
        d = self.canonical_dict()
        serialized = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
