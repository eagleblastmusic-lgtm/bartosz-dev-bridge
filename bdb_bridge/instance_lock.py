from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .models import BridgeErrorCode
from .protocol import BridgeError


class InstanceLock:
    def __init__(self, lock_file_path: Path) -> None:
        self.path = Path(lock_file_path).expanduser().resolve()
        self._file = None
        self._locked = False

    def acquire(self) -> bool:
        if self._locked:
            return True

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a+b")
            if self._file.tell() == 0:
                self._file.write(b"\x00")
                self._file.flush()
            self._file.seek(0)
        except OSError as exc:
            if self._file:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
            raise BridgeError(
                BridgeErrorCode.INSTANCE_LOCK_FAILED,
                f"Failed to open lock file: {exc}",
            ) from exc

        if sys.platform == "win32":
            import msvcrt
            import errno
            try:
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                self._locked = True
                return True
            except (OSError, IOError) as exc:
                self.close()
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise BridgeError(
                        BridgeErrorCode.INSTANCE_ALREADY_RUNNING,
                        f"Instance already running (lock held by another process): {exc}",
                    ) from exc
                else:
                    raise BridgeError(
                        BridgeErrorCode.INSTANCE_LOCK_FAILED,
                        f"Failed to acquire instance lock: {exc}",
                    ) from exc
        else:
            import fcntl
            import errno
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return True
            except (OSError, IOError) as exc:
                self.close()
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise BridgeError(
                        BridgeErrorCode.INSTANCE_ALREADY_RUNNING,
                        f"Instance already running (lock held by another process): {exc}",
                    ) from exc
                else:
                    raise BridgeError(
                        BridgeErrorCode.INSTANCE_LOCK_FAILED,
                        f"Failed to acquire instance lock: {exc}",
                    ) from exc

    def release(self) -> None:
        if not self._locked or not self._file:
            return
        try:
            self._file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._locked = False
            self.close()

    def close(self) -> None:
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()
