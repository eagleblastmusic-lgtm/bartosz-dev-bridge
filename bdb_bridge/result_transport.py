from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from .config import BridgeConfig
from .models import (
    BridgeErrorCode,
    PublishAttempt,
    PublishAttemptState,
    RemoteResult,
    RemoteResultState,
)
from .protocol import BridgeError, validate_base_sha, validate_repo_relative_path
from .recovery_journal import sha256_bytes


class ResultTransport(Protocol):
    def fetch_results_head(self) -> str: ...

    def read_result(self, remote_path: str) -> RemoteResult: ...

    def publish_result(
        self,
        *,
        remote_path: str,
        content: bytes,
        expected_results_head: str,
    ) -> PublishAttempt: ...


_SECRET_URL_RE = re.compile(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", re.IGNORECASE)


def _diagnostic(value: bytes | str | None, *, limit: int = 500) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value or ""
    text = _SECRET_URL_RE.sub(r"\1<redacted>@", text)
    text = " ".join(text.replace("\x00", "").split())
    return text[:limit]


class GitResultTransport:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        remote_name: str = "origin",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.repo_path = Path(config.control_repo_path)
        self.remote_name = remote_name
        self.results_ref = config.results_ref
        self.timeout_seconds = timeout_seconds
        self.results_branch = self._branch_from_ref(self.results_ref)
        self._observed_head: str | None = None

    def _branch_from_ref(self, value: str) -> str:
        prefixes = (f"{self.remote_name}/", f"refs/remotes/{self.remote_name}/", "refs/heads/")
        branch = value
        for prefix in prefixes:
            if branch.startswith(prefix):
                branch = branch[len(prefix):]
                break
        if not branch or branch.startswith("/") or ".." in branch or "\\" in branch:
            raise BridgeError(BridgeErrorCode.INVALID_CONFIG, "results_ref must identify a safe results branch")
        return branch

    @property
    def remote_tracking_ref(self) -> str:
        return f"refs/remotes/{self.remote_name}/{self.results_branch}"

    def _run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.repo_path,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout_seconds,
                env=env,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "Git result transport timed out") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Git result transport unavailable: {type(exc).__name__}",
            ) from exc

    def _ensure_repo(self) -> None:
        if not self.repo_path.is_dir():
            raise BridgeError(BridgeErrorCode.INVALID_CONTROL_REPO, "control_repo_path is not a directory")
        inside = self._run(["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != b"true":
            raise BridgeError(BridgeErrorCode.INVALID_CONTROL_REPO, "control_repo_path is not a Git work tree")
        remotes = self._run(["remote"])
        if remotes.returncode != 0:
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "Unable to list Git remotes")
        names = {line.decode("utf-8", errors="replace") for line in remotes.stdout.splitlines()}
        if self.remote_name not in names:
            raise BridgeError(BridgeErrorCode.INVALID_CONTROL_REPO, f"Git remote {self.remote_name!r} does not exist")

    def fetch_results_head(self) -> str:
        self._ensure_repo()
        fetch = self._run(
            [
                "fetch",
                "--no-tags",
                self.remote_name,
                f"refs/heads/{self.results_branch}:{self.remote_tracking_ref}",
            ]
        )
        if fetch.returncode != 0:
            raise BridgeError(
                BridgeErrorCode.TRANSPORT_UNAVAILABLE,
                f"Unable to fetch results ref: {_diagnostic(fetch.stderr)}",
            )
        resolved = self._run(["rev-parse", "--verify", self.remote_tracking_ref])
        if resolved.returncode != 0:
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "Fetched results ref cannot be resolved")
        try:
            head = validate_base_sha(resolved.stdout.decode("ascii", errors="strict").strip())
        except (UnicodeError, BridgeError) as exc:
            raise BridgeError(BridgeErrorCode.TRANSPORT_UNAVAILABLE, "Fetched results head is invalid") from exc
        self._observed_head = head
        return head

    def _read_at_head(self, remote_path: str, head: str) -> RemoteResult:
        validate_repo_relative_path(remote_path)
        try:
            head = validate_base_sha(head)
        except BridgeError as exc:
            return RemoteResult(RemoteResultState.UNAVAILABLE, remote_path, None, None, None, None, _diagnostic(exc))
        listing = self._run(["ls-tree", "-z", head, "--", remote_path])
        if listing.returncode != 0:
            return RemoteResult(RemoteResultState.UNAVAILABLE, remote_path, None, None, None, head, _diagnostic(listing.stderr))
        if not listing.stdout:
            return RemoteResult(RemoteResultState.ABSENT, remote_path, None, None, None, head)
        try:
            metadata, listed_path = listing.stdout.rstrip(b"\x00").split(b"\t", 1)
            _mode, kind, blob_sha = metadata.split(b" ", 2)
            if kind != b"blob" or listed_path.decode("utf-8", errors="strict") != remote_path:
                raise ValueError("unexpected tree entry")
            blob = blob_sha.decode("ascii", errors="strict")
        except (ValueError, UnicodeError) as exc:
            return RemoteResult(RemoteResultState.UNAVAILABLE, remote_path, None, None, None, head, f"Invalid Git tree entry: {type(exc).__name__}")
        content = self._run(["cat-file", "blob", blob])
        if content.returncode != 0:
            return RemoteResult(RemoteResultState.UNAVAILABLE, remote_path, None, None, None, head, _diagnostic(content.stderr))
        origin = self._run(["log", "-1", "--format=%H", head, "--", remote_path])
        commit_sha = head
        if origin.returncode == 0 and origin.stdout.strip():
            try:
                commit_sha = validate_base_sha(origin.stdout.decode("ascii", errors="strict").strip())
            except (UnicodeError, BridgeError):
                commit_sha = head
        return RemoteResult(RemoteResultState.PRESENT, remote_path, content.stdout, sha256_bytes(content.stdout), commit_sha, head)

    def read_result(self, remote_path: str) -> RemoteResult:
        try:
            head = self._observed_head or self.fetch_results_head()
            return self._read_at_head(remote_path, head)
        except BridgeError as exc:
            return RemoteResult(RemoteResultState.UNAVAILABLE, remote_path, None, None, None, self._observed_head, _diagnostic(exc))

    def publish_result(
        self,
        *,
        remote_path: str,
        content: bytes,
        expected_results_head: str,
    ) -> PublishAttempt:
        validate_repo_relative_path(remote_path)
        if not isinstance(content, bytes):
            raise BridgeError(BridgeErrorCode.INVALID_PAYLOAD, "result content must be bytes")
        expected_results_head = validate_base_sha(expected_results_head)
        try:
            self._ensure_repo()
            existing = self._read_at_head(remote_path, expected_results_head)
            if existing.state == RemoteResultState.PRESENT:
                if existing.content == content:
                    return PublishAttempt(PublishAttemptState.IDENTICAL, remote_path, expected_results_head, commit_sha=existing.commit_sha, remote_sha256=existing.content_sha256)
                return PublishAttempt(PublishAttemptState.COLLISION, remote_path, expected_results_head, commit_sha=existing.commit_sha, remote_sha256=existing.content_sha256, diagnostic="remote path already contains different exact bytes")
            if existing.state == RemoteResultState.UNAVAILABLE:
                return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic=existing.diagnostic)

            blob = self._run(["hash-object", "-w", "--stdin"], input_bytes=content)
            if blob.returncode != 0:
                return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic=_diagnostic(blob.stderr))
            blob_sha = blob.stdout.decode("ascii", errors="strict").strip()
            validate_base_sha(blob_sha)

            index_fd, index_name = tempfile.mkstemp(prefix="bdb-outbox-index-")
            os.close(index_fd)
            os.unlink(index_name)
            env = os.environ.copy()
            env.update({
                "GIT_INDEX_FILE": index_name,
                "GIT_AUTHOR_NAME": "Bartosz Dev Bridge",
                "GIT_AUTHOR_EMAIL": "bridge@localhost.invalid",
                "GIT_COMMITTER_NAME": "Bartosz Dev Bridge",
                "GIT_COMMITTER_EMAIL": "bridge@localhost.invalid",
            })
            try:
                read_tree = self._run(["read-tree", expected_results_head], env=env)
                if read_tree.returncode != 0:
                    return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic=_diagnostic(read_tree.stderr))
                update = self._run(["update-index", "--add", "--cacheinfo", "100644", blob_sha, remote_path], env=env)
                if update.returncode != 0:
                    return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic=_diagnostic(update.stderr))
                tree = self._run(["write-tree"], env=env)
                if tree.returncode != 0:
                    return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic=_diagnostic(tree.stderr))
                tree_sha = validate_base_sha(tree.stdout.decode("ascii", errors="strict").strip())
                message = f"bdb: publish result {remote_path}\n".encode("utf-8")
                commit = self._run(["commit-tree", tree_sha, "-p", expected_results_head], input_bytes=message, env=env)
                if commit.returncode != 0:
                    return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic=_diagnostic(commit.stderr))
                commit_sha = validate_base_sha(commit.stdout.decode("ascii", errors="strict").strip())
                changed = self._run(["diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha])
                names = [line.decode("utf-8", errors="strict") for line in changed.stdout.splitlines()]
                if changed.returncode != 0 or names != [remote_path]:
                    return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic="publisher commit did not contain exactly the result path")
                push = self._run(["push", "--porcelain", self.remote_name, f"{commit_sha}:refs/heads/{self.results_branch}"])
                if push.returncode == 0:
                    return PublishAttempt(PublishAttemptState.PUBLISHED, remote_path, expected_results_head, commit_sha=commit_sha, remote_sha256=sha256_bytes(content))
                detail = _diagnostic(push.stderr + b" " + push.stdout)
                lowered = detail.lower()
                state = PublishAttemptState.BRANCH_MOVED if any(token in lowered for token in ("non-fast-forward", "fetch first", "rejected", "stale info")) else PublishAttemptState.UNAVAILABLE
                return PublishAttempt(state, remote_path, expected_results_head, diagnostic=detail)
            finally:
                try:
                    os.unlink(index_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        except (BridgeError, UnicodeError, ValueError, OSError) as exc:
            return PublishAttempt(PublishAttemptState.UNAVAILABLE, remote_path, expected_results_head, diagnostic=_diagnostic(exc))
