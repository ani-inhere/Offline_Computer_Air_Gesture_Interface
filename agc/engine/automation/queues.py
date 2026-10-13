"""Per-worker job queue: a never-dropped release channel plus a bounded normal channel.

The lock here is the only one :meth:`ActionDispatcher.submit` shares with a
worker, and every critical section is a handful of deque operations (at most a
scan of ``maxsize`` entries for coalescing). No one holds it while a job runs.
"""

from __future__ import annotations

import threading
from collections import deque

from .jobs import Job


class JobQueue:
    """Two-channel FIFO. Workers always take release jobs before normal ones.

    Normal channel rules on :meth:`put`:

    * a job whose ``dedupe_key`` matches a queued job replaces it in place
      (newest value wins, queue position kept);
    * otherwise, if the channel is full, the oldest job is dropped.

    Displaced jobs are returned to the caller, which owes them a DROPPED result.
    """

    def __init__(self, maxsize: int = 32) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._release: deque[Job] = deque()
        self._normal: deque[Job] = deque()
        self._cond = threading.Condition(threading.Lock())

    def put(self, job: Job) -> list[tuple[Job, str]]:
        """Enqueue without blocking. Returns ``(displaced_job, reason)`` pairs."""
        displaced: list[tuple[Job, str]] = []
        with self._cond:
            if job.is_release:
                self._release.append(job)
            else:
                key = job.request.dedupe_key
                replaced = False
                if key is not None:
                    for i, queued in enumerate(self._normal):
                        if queued.request.dedupe_key == key:
                            self._normal[i] = job
                            displaced.append((queued, f"coalesced into {job.id}"))
                            replaced = True
                            break
                if not replaced:
                    if len(self._normal) >= self.maxsize:
                        displaced.append((self._normal.popleft(), "queue full, oldest dropped"))
                    self._normal.append(job)
            self._cond.notify()
        return displaced

    def get(self, timeout: float) -> Job | None:
        """Pop the next job (release channel first), waiting up to ``timeout`` seconds."""
        with self._cond:
            if not self._release and not self._normal:
                self._cond.wait(timeout)
            if self._release:
                return self._release.popleft()
            if self._normal:
                return self._normal.popleft()
            return None

    def remove(self, job: Job) -> bool:
        """Take a specific job out of the queue. Returns False if it was not queued."""
        with self._cond:
            for channel in (self._normal, self._release):
                try:
                    channel.remove(job)
                    return True
                except ValueError:
                    pass
            return False

    def drain_normal(self) -> list[Job]:
        """Remove and return every normal-priority job (release jobs stay)."""
        with self._cond:
            jobs = list(self._normal)
            self._normal.clear()
            return jobs

    def drain_release(self) -> list[Job]:
        """Remove and return every release job."""
        with self._cond:
            jobs = list(self._release)
            self._release.clear()
            return jobs

    def wake(self) -> None:
        """Wake a worker blocked in :meth:`get` (used on stop)."""
        with self._cond:
            self._cond.notify_all()

    def depth(self) -> tuple[int, int]:
        """``(release, normal)`` queue lengths."""
        with self._cond:
            return len(self._release), len(self._normal)
