"""
engine.py - ONE long-lived thread for everything that touches the session.

Why not Textual's @work(thread=True)? Each such job runs on a different thread, but Danica's
SQLite-backed objects (KnowledgeIndex, MemoryIndex) were built for the old CLI, where everything
ran on the main thread; sqlite3 refuses to be used from a thread other than the one that opened
it. A single engine thread gives them the same guarantee again. It also serialises the work, so a
reminder poll can never run in the middle of a request.

Jobs are plain callables. Dialogs opened by a job block THIS thread, never the UI thread.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable


class Engine:
    def __init__(self, on_error: Callable[[BaseException], None] | None = None) -> None:
        self._q: queue.Queue = queue.Queue()
        self._on_error = on_error or (lambda exc: None)
        self._thread = threading.Thread(target=self._loop, name="danica-engine", daemon=True)
        self.busy = False

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    @property
    def ident(self) -> int | None:
        return self._thread.ident

    def submit(self, fn: Callable, *args) -> None:
        self._q.put((fn, args))

    def stop(self, timeout: float = 3.0) -> None:
        """Let queued jobs finish, then end the thread (waits up to `timeout` seconds)."""
        self._q.put(None)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, args = item
            self.busy = True
            try:
                fn(*args)
            except BaseException as exc:          # a job must never kill the engine
                try:
                    self._on_error(exc)
                except Exception:
                    pass
            finally:
                self.busy = False