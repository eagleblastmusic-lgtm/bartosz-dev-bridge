from __future__ import annotations

import subprocess
from pathlib import Path

from bdb_bridge import BridgeConfig, GitResultTransport, PublishAttemptState, RemoteResultState


def run(cwd: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    control = tmp_path / "control"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    run(seed, "config", "user.name", "Test")
    run(seed, "config", "user.email", "test@example.invalid")
    (seed / "README.md").write_bytes(b"seed\n")
    run(seed, "add", "README.md")
    run(seed, "commit", "-m", "seed")
    run(seed, "branch", "-M", "results")
    run(seed, "push", "origin", "results")
    run(seed, "switch", "-c", "commands")
    run(seed, "push", "origin", "commands")
    subprocess.run(["git", "clone", "--branch", "results", str(bare), str(control)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    run(control, "config", "user.name", "Test")
    run(control, "config", "user.email", "test@example.invalid")
    return bare, control


def config(tmp_path: Path, control: Path) -> BridgeConfig:
    return BridgeConfig(
        control_repo_path=control,
        fixture_repo_path=tmp_path / "fixture",
        worktree_root=tmp_path / "worktrees",
        results_ref="origin/results",
    )


def test_git_transport_exact_bytes_and_idempotency(tmp_path: Path) -> None:
    bare, control = setup_repo(tmp_path)
    transport = GitResultTransport(config(tmp_path, control))
    head = transport.fetch_results_head()
    path = "sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/results/000001.json"
    content = b'{"value":"exact"}'
    before_head = run(control, "rev-parse", "HEAD").stdout
    before_status = run(control, "status", "--porcelain").stdout
    attempt = transport.publish_result(remote_path=path, content=content, expected_results_head=head)
    assert attempt.state == PublishAttemptState.PUBLISHED
    remote_bytes = subprocess.run(["git", "--git-dir", str(bare), "show", f"results:{path}"], check=True, stdout=subprocess.PIPE).stdout
    assert remote_bytes == content
    assert not remote_bytes.endswith(b"\n")
    assert run(control, "rev-parse", "HEAD").stdout == before_head
    assert run(control, "status", "--porcelain").stdout == before_status

    fresh = transport.fetch_results_head()
    remote = transport.read_result(path)
    assert remote.state == RemoteResultState.PRESENT and remote.content == content
    same = transport.publish_result(remote_path=path, content=content, expected_results_head=fresh)
    assert same.state == PublishAttemptState.IDENTICAL
    different = transport.publish_result(remote_path=path, content=b"different", expected_results_head=fresh)
    assert different.state == PublishAttemptState.COLLISION


def test_git_transport_branch_moved_is_non_destructive(tmp_path: Path) -> None:
    bare, control = setup_repo(tmp_path)
    transport = GitResultTransport(config(tmp_path, control))
    old_head = transport.fetch_results_head()
    mover = tmp_path / "mover"
    subprocess.run(["git", "clone", "--branch", "results", str(bare), str(mover)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    run(mover, "config", "user.name", "Mover")
    run(mover, "config", "user.email", "mover@example.invalid")
    (mover / "other.txt").write_bytes(b"move\n")
    run(mover, "add", "other.txt")
    run(mover, "commit", "-m", "move results")
    run(mover, "push", "origin", "results")

    path = "sessions/018f3f66-6cb3-4f66-9f2e-3d7647d1b701/results/000002.json"
    attempt = transport.publish_result(remote_path=path, content=b"{}", expected_results_head=old_head)
    assert attempt.state == PublishAttemptState.BRANCH_MOVED
    missing = subprocess.run(["git", "--git-dir", str(bare), "show", f"results:{path}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert missing.returncode != 0
