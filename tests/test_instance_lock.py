from __future__ import annotations

import sys
import time
import subprocess
from pathlib import Path
import pytest

from bdb_bridge import InstanceLock, BridgeError, BridgeErrorCode


def test_lock_exclusivity(tmp_path: Path) -> None:
    lock_file = tmp_path / "bridge.lock"
    
    # 1. Acquire first lock
    lock1 = InstanceLock(lock_file)
    assert lock1.acquire() is True
    
    # Same process double acquire is allowed / idempotent
    assert lock1.acquire() is True

    # 2. Try to acquire in another process, should fail
    # We will spawn a subprocess that attempts to acquire the lock and prints outcome
    code = f"""
import sys
from pathlib import Path
from bdb_bridge import InstanceLock, BridgeError

lock = InstanceLock(Path({repr(str(lock_file))}))
try:
    lock.acquire()
    print("ACQUIRED")
except BridgeError as exc:
    print("FAILED:", exc.code)
except Exception as exc:
    print("ERROR:", type(exc).__name__)
"""
    cmd = [sys.executable, "-c", code]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "FAILED: instance_already_running" in res.stdout.strip()

    # 3. Release first lock and acquire in subprocess
    lock1.release()
    
    code_acq = f"""
import sys
from pathlib import Path
from bdb_bridge import InstanceLock

lock = InstanceLock(Path({repr(str(lock_file))}))
if lock.acquire():
    print("ACQUIRED")
else:
    print("FAILED")
"""
    res2 = subprocess.run([sys.executable, "-c", code_acq], capture_output=True, text=True, check=True)
    assert res2.stdout.strip() == "ACQUIRED"


def test_lock_abrupt_subprocess_exit_releases(tmp_path: Path) -> None:
    lock_file = tmp_path / "bridge.lock"
    
    # Spawn a subprocess that acquires the lock, sleeps, then we kill it abruptly
    code = f"""
import time
from pathlib import Path
from bdb_bridge import InstanceLock

lock = InstanceLock(Path({repr(str(lock_file))}))
lock.acquire()
print("LOCKED", flush=True)
time.sleep(10)
"""
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    
    # Wait for child to lock
    line = proc.stdout.readline().strip()
    assert line == "LOCKED"
    
    # Attempting to lock locally should fail
    lock_local = InstanceLock(lock_file)
    with pytest.raises(BridgeError) as exc:
        lock_local.acquire()
    assert exc.value.code == BridgeErrorCode.INSTANCE_ALREADY_RUNNING
    
    # Terminate process abruptly
    proc.terminate()
    proc.wait()
    time.sleep(0.5)
    
    # Now local lock should succeed
    assert lock_local.acquire() is True
    lock_local.release()


def test_lock_invalid_path_mapped(tmp_path: Path) -> None:
    # Use a path that cannot be created (directory does not exist and cannot be created)
    # E.g. lock file under a file that exists
    dummy = tmp_path / "dummy_file"
    dummy.write_text("not a dir", encoding="utf-8")
    
    lock_file = dummy / "nested" / "bridge.lock"
    lock = InstanceLock(lock_file)
    with pytest.raises(BridgeError) as exc:
        lock.acquire()
    assert exc.value.code == BridgeErrorCode.JOURNAL_CONFLICT
