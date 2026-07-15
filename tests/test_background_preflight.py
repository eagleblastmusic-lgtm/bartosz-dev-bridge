from __future__ import annotations

from pathlib import Path

import pytest

from bdb_bridge import (
    BridgeError,
    BridgeErrorCode,
    Journal,
    ServiceStatus,
    ServiceStatusSnapshot,
)
from bdb_bridge import cli
from tests.helpers.service_lifecycle_fixture import make_service_config


def snapshot(status: ServiceStatus, *, lock_held: bool = False) -> ServiceStatusSnapshot:
    active = status in (ServiceStatus.RUNNING, ServiceStatus.STOPPING)
    return ServiceStatusSnapshot(
        status=status,
        instance_id="inst-77777777-7777-4777-8777-777777777777" if active else None,
        pid=1234 if active else None,
        started_at="2026-07-15T12:00:00Z" if active else None,
        heartbeat_at="2026-07-15T12:00:01Z" if active else None,
        heartbeat_age_seconds=0.1 if active else None,
        lock_held=lock_held,
        pid_alive=True if active else None,
        stop_requested=status == ServiceStatus.STOPPING,
        diagnostic=None,
    )


class FakeProcess:
    def poll(self):
        return None


def forbid_popen(*args, **kwargs):
    raise AssertionError("background child must not be created")


def test_corrupt_journal_preflight_does_not_spawn_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, config_path, _marker = make_service_config(tmp_path)
    Path(config.journal_path).write_bytes(b"not-a-sqlite-database")
    monkeypatch.setattr("subprocess.Popen", forbid_popen)

    assert cli.run_background(config, config_path) == 1
    err = capsys.readouterr().err
    assert "Background preflight failed" in err
    assert "journal" in err.lower()


def test_lock_backend_failure_preflight_does_not_spawn_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, config_path, _marker = make_service_config(tmp_path)
    journal = Journal.open(config.journal_path)
    journal.close()

    def fail_status(self, journal):
        raise BridgeError(BridgeErrorCode.INSTANCE_LOCK_FAILED, "lock backend failed")

    monkeypatch.setattr(cli.ServiceStatusReader, "get_status", fail_status)
    monkeypatch.setattr("subprocess.Popen", forbid_popen)

    assert cli.run_background(config, config_path) == 1
    err = capsys.readouterr().err
    assert "instance_lock_failed" in err
    assert "lock backend failed" in err


def test_normal_offline_preflight_starts_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, config_path, _marker = make_service_config(tmp_path)
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(cli, "_read_background_status", lambda _config: snapshot(ServiceStatus.RUNNING, lock_held=True))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    assert cli.run_background(config, config_path) == 0
    assert len(calls) == 1
    assert calls[0][1]["shell"] is False
    assert "started in background" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("state", [ServiceStatus.RUNNING, ServiceStatus.STOPPING])
def test_running_or_stopping_preflight_is_already_running_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: ServiceStatus,
) -> None:
    config, config_path, _marker = make_service_config(tmp_path)
    journal = Journal.open(config.journal_path)
    journal.close()
    monkeypatch.setattr(
        cli.ServiceStatusReader,
        "get_status",
        lambda self, journal: snapshot(state, lock_held=True),
    )
    monkeypatch.setattr("subprocess.Popen", forbid_popen)

    assert cli.run_background(config, config_path) == 1
    assert "already running" in capsys.readouterr().err.lower()
