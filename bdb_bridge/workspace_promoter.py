from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .config import BridgeConfig
from .protocol import BridgeError, path_matches, validate_repo_relative_path, validate_session_id
from .workspace_manager import Git, changed_paths


PROMOTION_RECEIPT_SCHEMA = "bdb-workspace-promotion-v1"
PROMOTER_STATE_SCHEMA = "bdb-workspace-promoter-state-v1"
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_MAX_CHANGED_PATHS = 200
_MAX_STATE_ENTRIES = 5_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class PromotionOutcome:
    status: str
    session_id: str
    sequence: int
    source_commit: str | None
    receipt_path: Path
    changed_files: tuple[str, ...]
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "source_commit": self.source_commit,
            "receipt_path": str(self.receipt_path),
            "changed_files": list(self.changed_files),
            "detail": self.detail,
        }


class WorkspacePromoter:
    """Promote one successful isolated result with a verified Git fast-forward."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        commit_name: str = "Bartosz Dev Bridge",
        commit_email: str = "bdb@localhost.invalid",
    ) -> None:
        self.config = config
        self.source = Path(config.fixture_repo_path).expanduser().resolve(strict=True)
        self.worktree_root = Path(config.worktree_root).expanduser().resolve(strict=False)
        self.result_root = Path(config.direct_result_dir).expanduser().resolve(strict=False)
        self.receipt_root = Path(config.runtime_dir).expanduser().resolve(strict=False) / "promotions"
        self.commit_name = commit_name
        self.commit_email = commit_email
        self.source_git = Git(self.source)

    def promote_file(self, result_path: str | Path) -> PromotionOutcome:
        path = Path(result_path).expanduser().resolve(strict=True)
        self._require_result_path(path)
        if path.is_symlink() or not path.is_file():
            raise BridgeError("unsafe_path", "Promotion result must be a regular file")
        data = path.read_bytes()
        if len(data) > _MAX_RESULT_BYTES:
            raise BridgeError("invalid_payload", "Promotion result exceeds the byte limit")
        try:
            document = json.loads(data.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("invalid_payload", "Promotion result is not strict UTF-8 JSON") from exc
        if not isinstance(document, dict):
            raise BridgeError("invalid_payload", "Promotion result must be an object")

        session_id, sequence, changed = self._validate_result(document)
        receipt = self._receipt_path(session_id, sequence)
        if receipt.exists():
            return self._read_existing_receipt(receipt, session_id, sequence, changed)

        worktree = self._worktree_path(session_id)
        worktree_git = Git(worktree)
        self._verify_registered_worktree(worktree, worktree_git)
        self._verify_source_branch()

        source_status = self.source_git.run(["status", "--porcelain=v1"]).stdout
        if source_status.strip():
            raise BridgeError("dirty_source_checkout", "Source checkout must be clean before promotion")

        worktree_status_text = worktree_git.run(["status", "--porcelain=v1"]).stdout
        worktree_changes = changed_paths(worktree_status_text)
        worktree_head = worktree_git.run(["rev-parse", "HEAD"]).stdout.strip().lower()
        source_head = self.source_git.run(["rev-parse", "HEAD"]).stdout.strip().lower()

        if worktree_changes:
            if sorted(worktree_changes) != list(changed):
                raise BridgeError(
                    "manual_reconciliation_required",
                    f"Worktree changes differ from durable result: {worktree_changes[:20]}",
                )
            if source_head != worktree_head:
                raise BridgeError(
                    "manual_reconciliation_required",
                    "Source HEAD no longer matches the isolated workspace base",
                )
            worktree_git.run(["diff", "--check", "--", *changed])
            worktree_git.run(["add", "-A", "--", *changed])
            staged = worktree_git.run(["diff", "--cached", "--name-only"]).stdout.splitlines()
            staged = sorted(value.replace("\\", "/") for value in staged if value)
            if staged != list(changed):
                raise BridgeError(
                    "manual_reconciliation_required",
                    f"Staged paths differ from durable result: {staged[:20]}",
                )
            command_id = str(document.get("command_id") or f"{session_id}:{sequence:06d}")
            worktree_git.run(
                [
                    "-c",
                    f"user.name={self.commit_name}",
                    "-c",
                    f"user.email={self.commit_email}",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    f"bdb: promote {command_id}",
                ]
            )
            commit_sha = worktree_git.run(["rev-parse", "HEAD"]).stdout.strip().lower()
            parent_sha = self._single_parent(worktree_git, commit_sha)
            if parent_sha != source_head:
                raise BridgeError(
                    "manual_reconciliation_required",
                    "Promotion commit parent differs from source HEAD",
                )
        else:
            commit_sha = worktree_head
            parent_sha = self._single_parent(worktree_git, commit_sha)
            committed = worktree_git.run(
                ["diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha]
            ).stdout.splitlines()
            committed = sorted(value.replace("\\", "/") for value in committed if value)
            if committed != list(changed):
                raise BridgeError(
                    "manual_reconciliation_required",
                    "Clean worktree commit does not match the durable result",
                )
            if source_head not in {parent_sha, commit_sha}:
                raise BridgeError(
                    "manual_reconciliation_required",
                    "Source HEAD is neither the promotion parent nor the promoted commit",
                )

        if source_head == parent_sha:
            self.source_git.run(["merge", "--ff-only", commit_sha])
        elif source_head != commit_sha:
            raise BridgeError("manual_reconciliation_required", "Source checkout cannot be fast-forwarded safely")

        final_head = self.source_git.run(["rev-parse", "HEAD"]).stdout.strip().lower()
        final_status = self.source_git.run(["status", "--porcelain=v1"]).stdout
        if final_head != commit_sha or final_status.strip():
            raise BridgeError(
                "manual_reconciliation_required",
                "Source checkout differs after fast-forward promotion",
            )

        file_hashes: dict[str, str | None] = {}
        for relative in changed:
            target = self._source_path(relative)
            file_hashes[relative] = _sha256(target.read_bytes()) if target.is_file() else None

        receipt_document = {
            "schema": PROMOTION_RECEIPT_SCHEMA,
            "status": "promoted",
            "session_id": session_id,
            "sequence": sequence,
            "command_id": document.get("command_id"),
            "result_sha256": _sha256(data),
            "source_commit": commit_sha,
            "parent_commit": parent_sha,
            "changed_files": list(changed),
            "file_sha256": file_hashes,
            "promoted_at": _utc_now(),
        }
        _atomic_json(receipt, receipt_document)
        return PromotionOutcome("promoted", session_id, sequence, commit_sha, receipt, changed)

    def _validate_result(self, document: dict[str, Any]) -> tuple[str, int, tuple[str, ...]]:
        if document.get("status") != "success" or document.get("exit_code") != 0:
            raise BridgeError("policy_denied", "Only successful zero-exit results can be promoted")
        session_id = document.get("session_id")
        if not isinstance(session_id, str):
            raise BridgeError("invalid_payload", "Promotion result has no session_id")
        validate_session_id(session_id)
        sequence = document.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != 1:
            raise BridgeError("policy_denied", "Automatic promotion requires a fresh sequence-1 session")
        data = document.get("data")
        if not isinstance(data, dict):
            raise BridgeError("invalid_payload", "Promotion result has no data object")
        if data.get("operation") != "multi_file_patch":
            raise BridgeError("policy_denied", "Automatic promotion supports only multi_file_patch")
        if data.get("checkpoint_state") != "committed" or data.get("rollback_performed") is not False:
            raise BridgeError("policy_denied", "Promotion requires a committed checkpoint without rollback")
        changed = document.get("changed_files")
        if (
            not isinstance(changed, list)
            or not changed
            or len(changed) > _MAX_CHANGED_PATHS
            or not all(isinstance(value, str) for value in changed)
        ):
            raise BridgeError("invalid_payload", "Promotion changed_files is invalid")
        normalized: list[str] = []
        for value in changed:
            path = validate_repo_relative_path(value.replace("\\", "/"))
            if not path_matches(path, self.config.allowed_paths):
                raise BridgeError("policy_denied", f"Promotion path is outside local policy: {path}")
            normalized.append(path)
        if len(set(normalized)) != len(normalized):
            raise BridgeError("invalid_payload", "Promotion changed_files contains duplicates")
        return session_id, sequence, tuple(sorted(normalized))

    def _require_result_path(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.result_root)
        except ValueError as exc:
            raise BridgeError("unsafe_path", "Promotion result escaped the configured result root") from exc
        parts = relative.parts
        if len(parts) != 4 or parts[0] != "sessions" or parts[2] != "results" or not parts[3].endswith(".json"):
            raise BridgeError("unsafe_path", "Promotion result path is not canonical")

    def _worktree_path(self, session_id: str) -> Path:
        path = (self.worktree_root / session_id).resolve(strict=True)
        if path.parent != self.worktree_root or path.is_symlink() or not path.is_dir():
            raise BridgeError("unsafe_worktree_path", "Promotion worktree path is unsafe")
        return path

    def _source_path(self, relative: str) -> Path:
        normalized = validate_repo_relative_path(relative)
        candidate = self.source.joinpath(*PurePosixPath(normalized).parts).resolve(strict=False)
        try:
            candidate.relative_to(self.source)
        except ValueError as exc:
            raise BridgeError("unsafe_path", f"Promotion path escaped source checkout: {normalized}") from exc
        if candidate.is_symlink():
            raise BridgeError("unsafe_path", f"Promotion path must not be a symlink: {normalized}")
        return candidate

    def _verify_registered_worktree(self, worktree: Path, worktree_git: Git) -> None:
        expected = worktree.resolve(strict=True)
        entries = self.source_git.run(["worktree", "list", "--porcelain"]).stdout.splitlines()
        matches = 0
        current_path: Path | None = None
        detached = False
        for line in entries + [""]:
            if line.startswith("worktree "):
                current_path = Path(line.removeprefix("worktree ")).resolve(strict=False)
                detached = False
            elif line == "detached":
                detached = True
            elif not line and current_path is not None:
                if current_path == expected:
                    if not detached:
                        raise BridgeError("manual_reconciliation_required", "Promotion workspace is not detached")
                    matches += 1
                current_path = None
        if matches != 1:
            raise BridgeError("manual_reconciliation_required", "Promotion workspace is not exactly one registered worktree")
        symbolic = worktree_git.run(["symbolic-ref", "-q", "HEAD"], check=False)
        if symbolic.returncode == 0:
            raise BridgeError("manual_reconciliation_required", "Promotion workspace HEAD is attached")

    def _verify_source_branch(self) -> None:
        branch = self.source_git.run(["symbolic-ref", "-q", "--short", "HEAD"], check=False)
        if branch.returncode != 0 or not branch.stdout.strip():
            raise BridgeError("policy_denied", "Source checkout must be attached to a local branch")

    @staticmethod
    def _single_parent(git: Git, commit_sha: str) -> str:
        values = git.run(["rev-list", "--parents", "-n", "1", commit_sha]).stdout.strip().split()
        if len(values) != 2 or values[0].lower() != commit_sha.lower():
            raise BridgeError("manual_reconciliation_required", "Promotion commit must have exactly one parent")
        return values[1].lower()

    def _receipt_path(self, session_id: str, sequence: int) -> Path:
        return self.receipt_root / f"{session_id}-{sequence:06d}.json"

    @staticmethod
    def _read_existing_receipt(
        receipt: Path,
        session_id: str,
        sequence: int,
        changed: tuple[str, ...],
    ) -> PromotionOutcome:
        if receipt.is_symlink() or not receipt.is_file():
            raise BridgeError("unsafe_path", "Promotion receipt must be a regular file")
        raw = json.loads(receipt.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != PROMOTION_RECEIPT_SCHEMA
            or raw.get("session_id") != session_id
            or raw.get("sequence") != sequence
            or tuple(raw.get("changed_files") or ()) != changed
        ):
            raise BridgeError("journal_conflict", "Promotion receipt does not match the durable result")
        commit = raw.get("source_commit")
        if not isinstance(commit, str):
            raise BridgeError("journal_conflict", "Promotion receipt has no source commit")
        return PromotionOutcome("already_promoted", session_id, sequence, commit, receipt, changed)


class WorkspacePromotionWatcher:
    """Process each newly staged local result at most once."""

    def __init__(self, promoter: WorkspacePromoter) -> None:
        self.promoter = promoter
        self.state_path = Path(promoter.config.runtime_dir) / "workspace-promoter-state.json"

    def initialize_existing(self) -> int:
        state = self._read_state()
        if state["initialized"] is True:
            return 0
        count = 0
        for result in self._result_files():
            state["seen"][self._key(result)] = {
                "status": "ignored_existing",
                "result_sha256": _sha256(result.read_bytes()),
                "recorded_at": _utc_now(),
            }
            count += 1
        state["initialized"] = True
        self._write_state(state)
        return count

    def scan_once(self) -> list[PromotionOutcome]:
        state = self._read_state()
        if state["initialized"] is not True:
            self.initialize_existing()
            state = self._read_state()
        outcomes: list[PromotionOutcome] = []
        for result in self._result_files():
            key = self._key(result)
            if key in state["seen"]:
                continue
            result_hash = _sha256(result.read_bytes())
            try:
                outcome = self.promoter.promote_file(result)
                outcomes.append(outcome)
                entry = {
                    "status": outcome.status,
                    "source_commit": outcome.source_commit,
                    "result_sha256": result_hash,
                    "recorded_at": _utc_now(),
                }
            except Exception as exc:
                code = getattr(exc, "code", None)
                entry = {
                    "status": "blocked",
                    "error_code": str(getattr(code, "value", code) or type(exc).__name__),
                    "detail": str(exc)[:500],
                    "result_sha256": result_hash,
                    "recorded_at": _utc_now(),
                }
            state["seen"][key] = entry
            self._trim_state(state)
            self._write_state(state)
        return outcomes

    def _result_files(self) -> list[Path]:
        if not self.promoter.result_root.exists():
            return []
        return sorted(
            path.resolve(strict=True)
            for path in self.promoter.result_root.glob("sessions/*/results/*.json")
            if path.is_file() and not path.is_symlink()
        )

    def _key(self, path: Path) -> str:
        return path.relative_to(self.promoter.result_root).as_posix()

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema": PROMOTER_STATE_SCHEMA, "initialized": False, "seen": {}}
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise BridgeError("invalid_config", "Workspace promoter state must be a regular file")
        raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("schema") != PROMOTER_STATE_SCHEMA:
            raise BridgeError("unsupported_schema", "Workspace promoter state schema is unsupported")
        if not isinstance(raw.get("seen"), dict) or not isinstance(raw.get("initialized"), bool):
            raise BridgeError("invalid_config", "Workspace promoter state is invalid")
        return raw

    def _write_state(self, state: dict[str, Any]) -> None:
        self._trim_state(state)
        _atomic_json(self.state_path, state)

    @staticmethod
    def _trim_state(state: dict[str, Any]) -> None:
        seen = state["seen"]
        if len(seen) <= _MAX_STATE_ENTRIES:
            return
        ordered = sorted(
            seen.items(),
            key=lambda item: str(item[1].get("recorded_at", "")) if isinstance(item[1], dict) else "",
        )
        state["seen"] = dict(ordered[-_MAX_STATE_ENTRIES:])
