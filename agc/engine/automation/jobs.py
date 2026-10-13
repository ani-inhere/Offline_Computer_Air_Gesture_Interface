"""Job: a submitted request plus its lifecycle state and cancel token."""

from __future__ import annotations

import threading
import time
from enum import Enum

from .cancel import CancelToken
from .models import ActionRequest, Priority, Tier


class JobState(Enum):
    """QUEUED -> RUNNING -> DONE, or QUEUED -> DONE (dropped/cancelled/timed out while queued)."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"


class Job:
    """Tracks one request through the dispatcher.

    State changes go through :meth:`start` and :meth:`finish`, which are atomic,
    so exactly one party (worker, watchdog, cancel or queue overflow) gets to
    publish the job's result.
    """

    __slots__ = ("request", "tier", "token", "submitted_ts", "started_ts", "_state", "_lock")

    def __init__(self, request: ActionRequest, tier: Tier) -> None:
        self.request = request
        self.tier = tier
        self.token = CancelToken()
        self.submitted_ts = time.monotonic()
        self.started_ts: float | None = None
        self._state = JobState.QUEUED
        self._lock = threading.Lock()

    @property
    def id(self) -> str:
        return self.request.id

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def is_release(self) -> bool:
        return self.request.priority is Priority.RELEASE

    @property
    def deadline(self) -> float:
        """Monotonic time after which the job is timed out."""
        return self.submitted_ts + self.request.timeout_s

    def start(self) -> bool:
        """Move QUEUED -> RUNNING. Returns False if the job already finished."""
        with self._lock:
            if self._state is not JobState.QUEUED:
                return False
            self._state = JobState.RUNNING
            self.started_ts = time.monotonic()
            return True

    def finish(self) -> bool:
        """Move to DONE. Returns True only for the first caller (the one that publishes the result)."""
        with self._lock:
            if self._state is JobState.DONE:
                return False
            self._state = JobState.DONE
            return True

    def __repr__(self) -> str:
        return f"Job({self.request.op!r}, id={self.id[:8]}, tier={self.tier.value}, {self._state.value})"
