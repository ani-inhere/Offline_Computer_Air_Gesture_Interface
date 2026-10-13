"""Watchdog thread: runs callbacks at monotonic deadlines (job timeouts, stuck checks)."""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Callable

log = logging.getLogger("agc.automation.watchdog")


class Watchdog:
    """Single thread with a deadline heap.

    :meth:`schedule` is O(log n) under a short lock and never waits, so it is
    safe to call from the decision loop. Callbacks run on the watchdog thread
    and must be quick; they are expected to tolerate the job having finished
    already (entries are not removed early).
    """

    def __init__(self, name: str = "agc-automation-watchdog") -> None:
        self._heap: list[tuple[float, int, Callable[[], None]]] = []
        self._seq = itertools.count()
        self._cond = threading.Condition(threading.Lock())
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        """Start the thread (no-op if already started)."""
        if not self._thread.is_alive() and not self._stopped:
            self._thread.start()

    def schedule(self, when: float, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the watchdog thread at monotonic time ``when``."""
        with self._cond:
            heapq.heappush(self._heap, (when, next(self._seq), fn))
            if self._heap[0][2] is fn:
                self._cond.notify()

    def stop(self, timeout: float = 1.0) -> bool:
        """Stop without running pending callbacks. Returns True if the thread exited."""
        with self._cond:
            self._stopped = True
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def pending(self) -> int:
        """Number of scheduled entries (including stale ones)."""
        with self._cond:
            return len(self._heap)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopped:
                    if not self._heap:
                        self._cond.wait()
                        continue
                    delay = self._heap[0][0] - time.monotonic()
                    if delay <= 0:
                        break
                    self._cond.wait(delay)
                if self._stopped:
                    return
                now = time.monotonic()
                due = []
                while self._heap and self._heap[0][0] <= now:
                    due.append(heapq.heappop(self._heap)[2])
            for fn in due:
                try:
                    fn()
                except Exception:  # noqa: BLE001 - a bad callback must not kill the watchdog
                    log.exception("watchdog callback failed")
