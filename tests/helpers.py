"""Test helpers: result collection, request builders, polling."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from agc.engine.automation import ActionRequest, ActionResult, ActionType


class ResultCollector:
    """Result callback that records results and the thread each was delivered on."""

    def __init__(self) -> None:
        self.results: list[ActionResult] = []
        self.thread_names: list[str] = []
        self._cond = threading.Condition()

    def __call__(self, result: ActionResult) -> None:
        with self._cond:
            self.results.append(result)
            self.thread_names.append(threading.current_thread().name)
            self._cond.notify_all()

    def for_id(self, job_id: str) -> list[ActionResult]:
        with self._cond:
            return [r for r in self.results if r.id == job_id]

    def wait_for(self, job_id: str, timeout: float = 3.0) -> ActionResult:
        """Block until ``job_id`` has a result; fail the test on timeout."""
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                for result in self.results:
                    if result.id == job_id:
                        return result
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no result for {job_id} within {timeout}s")
                self._cond.wait(remaining)

    def wait_all(self, job_ids: list[str], timeout: float = 3.0) -> list[ActionResult]:
        end = time.monotonic() + timeout
        return [self.wait_for(job_id, max(0.0, end - time.monotonic())) for job_id in job_ids]


def make_request(op: str = "noop", type: ActionType = ActionType.OS, **kwargs: Any) -> ActionRequest:
    """Build an ActionRequest with test-friendly defaults."""
    return ActionRequest(op=op, type=type, **kwargs)


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.005) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
