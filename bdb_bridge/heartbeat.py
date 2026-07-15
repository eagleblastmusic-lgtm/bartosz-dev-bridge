from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .journal import Journal


class HeartbeatWorker:
    def __init__(
        self,
        journal_path: Path,
        instance_id: str,
        interval_seconds: float,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        self.journal_path = Path(journal_path).expanduser().resolve()
        self.instance_id = instance_id
        self.interval_seconds = interval_seconds
        self.now_fn = now_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self.instance_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        self._thread = None

    def get_error(self) -> Exception | None:
        return self._error

    def _run(self) -> None:
        journal: Journal | None = None
        try:
            journal = Journal.open(self.journal_path, now_fn=self.now_fn)
            while not self._stop_event.is_set():
                try:
                    journal.heartbeat_service_instance(self.instance_id)
                except Exception as exc:
                    self._error = exc
                    break

                elapsed = 0.0
                while elapsed < self.interval_seconds:
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.1)
                    elapsed += 0.1
        except Exception as exc:
            self._error = exc
        finally:
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    pass
