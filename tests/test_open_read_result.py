from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from bdb_bridge import (
    BridgeConfig,
    BridgeError,
    CommandIngestor,
    CommandState,
    Journal,
    LocalSpoolTransport,
    RemoteResult,
    RemoteResultState,
    ResultCoordinator,
    SingleQueueScheduler,
)
from bdb_bridge.local_mirroring_outbox import LocalMirroringOutboxProcessor
from bdb_bridge.local_result_sink import LocalResultSink
from bdb_bridge.native_actions import ACTION_SCHEMA
from bdb_bridge.native_host import (
    NATIVE_CONFIG_SCHEMA,
    NATIVE_REQUEST_SCHEMA,
    NativeArmStore,
    NativeHostConfig,
    NativeHostService,
)
from bdb_bridge.protocol import result_path_for


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
ALIAS = "pilot"
NOW = datetime(2026, 7, 17, 3, 0, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-07-17T03:00:01.000000Z"


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def initialize_repo(path: Path) -> tuple[str, str]:
    run_git(path, "init")
    run_git(path, "config", "user.name", "Open Read Test")
    run_git(path, "config", "user.email", "open-read@example.invalid")
    content = "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
    (path / "src").mkdir()
    (path / "src" / "clamp.py").write_text(content, encoding="utf-8", newline="\n")
    run_git(path, "add", "--", "src/clamp.py")
    run_git(path, "commit", "-m", "fixture")
    return run_git(path, "rev-parse", "HEAD"), content


class OfflineTransport:
    def fetch_results_head(self) -> str:
        raise BridgeError("transport_unavailable", "offline")

    def read_result(self, remote_path: str) -> RemoteResult:
        return RemoteResult(
            RemoteResultState.UNAVAILABLE,
            remote_path,
            None,
            None,
            None,
            None,
            "offline",
        )

    def publish_result(self, **kwargs):
        raise AssertionError("offline transport must not publish")


def write_configs(tmp_path: Path) -> tuple[BridgeConfig, Path, str, str]:
    control = tmp_path / "control"
    fixture = tmp_path / "fixture"
    worktrees = tmp_path / "worktrees"
    runtime = tmp_path / "runtime"
    for path in (control, fixture, worktrees, runtime):
        path.mkdir()
    base_sha, content = initialize_repo(fixture)
    bridge_path = tmp_path / "bridge.json"
    bridge_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "control_repo_path": str(control),
                "fixture_repo_path": str(fixture),
                "worktree_root": str(worktrees),
                "runtime_dir": str(runtime),
                "journal_path": str(runtime / "journal.db"),
                "repository_id": "bdb-open-read-test",
                "allowed_paths": ["src/clamp.py"],
                "python_executable": sys.executable,
                "direct_spool_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    native_path = tmp_path / "native-host.json"
    native_path.write_text(
        json.dumps(
            {
                "schema": NATIVE_CONFIG_SCHEMA,
                "repositories": {ALIAS: {"bridge_config_path": str(bridge_path)}},
                "allowed_origins": [ORIGIN],
                "state_path": str(tmp_path / "native-host-arm.json"),
                "session_store_path": str(tmp_path / "native-host-sessions.json"),
                "max_wait_seconds": 1,
                "max_message_bytes": 65536,
            }
        ),
        encoding="utf-8",
    )
    return BridgeConfig.from_json(bridge_path), native_path, base_sha, content


def test_native_open_read_reaches_durable_local_result_without_mutation(tmp_path: Path) -> None:
    config, native_path, base_sha, expected_content = write_configs(tmp_path)
    native_config = NativeHostConfig.from_json(native_path)
    NativeArmStore(native_config.state_path, now_fn=lambda: NOW).arm(minutes=5)
    host = NativeHostService(native_config, origin=ORIGIN, now_fn=lambda: NOW)

    accepted = host.handle(
        {
            "schema": NATIVE_REQUEST_SCHEMA,
            "request_id": "open-read-regression",
            "action": "submit_action",
            "wait_seconds": 0,
            "bdb_action": {
                "schema": ACTION_SCHEMA,
                "repo_alias": ALIAS,
                "operation": "open_read",
                "expected_revision": 0,
                "payload": {"path": "src/clamp.py"},
            },
        }
    )
    assert accepted["status"] == "accepted"
    command_id = accepted["command_id"]
    session_id, sequence_text = command_id.split(":")
    assert sequence_text == "000001"

    journal = Journal.open(config.journal_path, now_fn=lambda: NOW_TEXT)
    try:
        report = CommandIngestor(
            journal,
            LocalSpoolTransport(config.direct_spool_dir),
            source_id="local-spool",
            now_fn=lambda: NOW_TEXT,
        ).poll_once()
        assert report.ingestion is not None
        assert report.ingestion.commands_validated == 1

        claimed = SingleQueueScheduler(journal).claim_next()
        assert claimed is not None
        assert claimed.command_id == command_id
        assert claimed.state is CommandState.CLAIMED

        sink = LocalResultSink(config.direct_result_dir)
        outbox = LocalMirroringOutboxProcessor(
            journal,
            OfflineTransport(),
            now_fn=lambda: NOW_TEXT,
            result_sink=sink,
        )
        outcome = ResultCoordinator(
            config,
            journal,
            outbox,
            now_fn=lambda: NOW_TEXT,
        ).process(command_id)

        assert outcome.staged is True
        assert journal.get_command(command_id).state is CommandState.RESULT_STAGED
        assert journal.get_operation_plan(command_id) is None
        assert journal.get_operation_effect(command_id) is None
        workspace = journal.get_workspace(session_id)
        assert workspace is not None
        assert workspace.revision == 0

        result_bytes = sink.read(result_path_for(session_id, 1))
        assert result_bytes is not None
        result = json.loads(result_bytes.decode("utf-8"))
        assert result["status"] == "success"
        assert result["error_code"] is None
        assert result["changed_files"] == []
        assert result["workspace_revision_before"] == 0
        assert result["workspace_revision_after"] == 0
        assert result["state_hash_before"] == result["state_hash_after"]
        assert result["data"]["operation"] == "open_read"
        assert result["data"]["path"] == "src/clamp.py"
        assert result["data"]["content"] == expected_content
        assert result["data"]["file_bytes"] == len(expected_content.encode("utf-8"))
        assert result["end_marker"].startswith("BDB-END:sha256:")

        assert run_git(Path(config.fixture_repo_path), "status", "--porcelain=v1") == ""
        assert run_git(Path(workspace.workspace_path), "status", "--porcelain=v1") == ""
        assert run_git(Path(config.fixture_repo_path), "rev-parse", "HEAD") == base_sha
    finally:
        journal.close()
