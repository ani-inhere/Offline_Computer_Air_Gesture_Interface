"""ActionDispatcher: the one entry point between the decision loop and automation.

Threads::

    decision loop --submit()--> JobQueue(tier) --> tier worker --> adapter
          |                                            |
          +--schedule(deadline)--> Watchdog            +--> _finish() --> ResultNotifier --> callback
                                    (timeouts, stuck)

``submit`` only appends to a deque, pushes a heap entry and stores the job in
a dict, each under its own short lock that no thread holds while executing a
job. It never waits on a worker.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import partial
from typing import Any, Mapping

from .adapters.base import BaseAdapter
from .jobs import Job, JobState
from .models import ActionRequest, ActionResult, ActionType, Backend, Priority, Status, Tier
from .notifier import ResultCallback, ResultNotifier
from .queues import JobQueue
from .watchdog import Watchdog
from .workers import BaseWorker, ComWorker, GuiWorker, OsWorker, WorkerHealth

log = logging.getLogger("agc.automation.dispatcher")

DEFAULT_WORKERS: Mapping[Tier, type[BaseWorker]] = {
    Tier.OS: OsWorker,
    Tier.APP_API: ComWorker,
    Tier.GUI: GuiWorker,
}

_BACKEND_TIER = {Backend.OS: Tier.OS, Backend.APP_API: Tier.APP_API, Backend.GUI: Tier.GUI}
_TYPE_TIER = {ActionType.OS: Tier.OS, ActionType.APP_API: Tier.APP_API, ActionType.GUI_MACRO: Tier.GUI}


class ActionDispatcher:
    """Queues requests per tier, enforces timeouts, and publishes exactly one result per job.

    Args:
        queue_size: capacity of each tier's normal-priority channel.
        stuck_grace_s: how long a timed-out job may keep running before its
            worker is reported STUCK (new requests for that tier then fail fast
            as UNAVAILABLE until the job returns).
        worker_classes: override the worker class per tier (tests).
        worker_options: keyword arguments passed to every worker.
    """

    def __init__(
        self,
        *,
        queue_size: int = 32,
        stuck_grace_s: float = 1.0,
        worker_classes: Mapping[Tier, type[BaseWorker]] | None = None,
        worker_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._queue_size = queue_size
        self._stuck_grace_s = stuck_grace_s
        self._worker_classes = dict(DEFAULT_WORKERS)
        if worker_classes:
            self._worker_classes.update(worker_classes)
        self._worker_options = dict(worker_options or {})
        self._workers: dict[Tier, BaseWorker] = {}
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()
        self._notifier = ResultNotifier()
        self._watchdog = Watchdog()
        self._started = False
        self._closed = False

    # --------------------------------------------------------------- setup

    def register_backend(self, tier: Tier, adapter: BaseAdapter) -> None:
        """Attach ``adapter`` to ``tier``'s worker, creating the worker on first use."""
        if self._closed:
            raise RuntimeError("dispatcher is shut down")
        worker = self._workers.get(tier)
        if worker is None:
            worker_cls = self._worker_classes[tier]
            worker = worker_cls(JobQueue(self._queue_size), self._on_worker_result, **self._worker_options)
            self._workers[tier] = worker
            if self._started:
                worker.start()
        worker.add_adapter(adapter)

    def set_result_callback(self, callback: ResultCallback | None) -> None:
        """Set the function that receives every ActionResult (on the notifier thread)."""
        self._notifier.set_callback(callback)

    def start(self) -> None:
        """Start the notifier, watchdog and all registered workers. Idempotent."""
        if self._closed:
            raise RuntimeError("dispatcher is shut down")
        if self._started:
            return
        self._started = True
        self._notifier.start()
        self._watchdog.start()
        for worker in self._workers.values():
            worker.start()

    # ------------------------------------------------------------- hot path

    def submit(self, request: ActionRequest) -> str:
        """Queue ``request`` without blocking and return its job id.

        Every call produces exactly one result through the callback, including
        when the tier has no backend (UNAVAILABLE) or the job is displaced
        from a full queue (DROPPED). Submits after :meth:`shutdown` are ignored
        and logged.
        """
        if self._closed:
            log.warning("submit after shutdown ignored: op=%s", request.op)
            return request.id
        tier = self._tier_for(request)
        worker = self._workers.get(tier)
        if worker is None:
            self._notifier.emit(ActionResult.for_request(
                request, Status.UNAVAILABLE, f"no {tier.value} backend registered"))
            return request.id
        if worker.health is WorkerHealth.STUCK and request.priority is not Priority.RELEASE:
            self._notifier.emit(ActionResult.for_request(
                request, Status.UNAVAILABLE, f"{tier.value} worker is stuck on a previous job"))
            return request.id

        job = Job(request, tier)
        with self._jobs_lock:
            self._jobs[job.id] = job
        self._watchdog.schedule(job.deadline, partial(self._on_deadline, job))
        for displaced, reason in worker.queue.put(job):
            self._finish(displaced, ActionResult.for_request(displaced.request, Status.DROPPED, reason))
        return job.id

    # --------------------------------------------------------- cancellation

    def cancel(self, job_id: str) -> bool:
        """Cancel one job (queued or running). Returns False if it already finished."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        return self._cancel_job(job, "cancelled")

    def cancel_all(self) -> int:
        """Cancel every pending normal-priority job. Release jobs are kept. Returns the count."""
        with self._jobs_lock:
            jobs = [job for job in self._jobs.values() if not job.is_release]
        return sum(self._cancel_job(job, "cancelled") for job in jobs)

    def shutdown(self, timeout: float = 3.0) -> bool:
        """Cancel pending work, run queued release jobs, stop all threads.

        Returns True if every worker thread exited within ``timeout``. Jobs
        still unfinished afterwards get a CANCELLED result.
        """
        if self._closed:
            return True
        self._closed = True
        deadline = time.monotonic() + timeout
        self.cancel_all()
        for worker in self._workers.values():
            worker.stop()
        clean = True
        for worker in self._workers.values():
            clean &= worker.join(max(0.0, deadline - time.monotonic()))
        with self._jobs_lock:
            leftovers = list(self._jobs.values())
        for job in leftovers:
            job.token.cancel("shutdown")
            self._finish(job, ActionResult.for_request(job.request, Status.CANCELLED, "dispatcher shut down"))
        self._watchdog.stop(max(0.1, deadline - time.monotonic()))
        self._notifier.stop(max(0.1, deadline - time.monotonic()))
        if not clean:
            log.warning("shutdown: some workers did not exit within %.1fs", timeout)
        return clean

    # --------------------------------------------------------- diagnostics

    def worker(self, tier: Tier) -> BaseWorker | None:
        """The worker for ``tier``, if any backend is registered for it."""
        return self._workers.get(tier)

    def stats(self) -> dict[str, Any]:
        """Queue depths, worker health and pending job count (for the Debug HUD)."""
        workers = {}
        for tier, worker in self._workers.items():
            release, normal = worker.queue.depth()
            current = worker.current_job
            workers[tier.value] = {
                "health": worker.health.value,
                "queue_release": release,
                "queue_normal": normal,
                "restarts": worker.restart_count,
                "current_op": current.request.op if current else None,
                "adapters": [adapter.name for adapter in worker.adapters],
            }
        with self._jobs_lock:
            pending = len(self._jobs)
        return {"workers": workers, "pending_jobs": pending}

    # ------------------------------------------------------------ internals

    @staticmethod
    def _tier_for(request: ActionRequest) -> Tier:
        # Placeholder until the Phase 4 router: explicit backend, else by action type.
        if request.backend is not Backend.AUTO:
            return _BACKEND_TIER[request.backend]
        return _TYPE_TIER[request.type]

    def _finish(self, job: Job, result: ActionResult) -> bool:
        """Publish ``result`` if nobody else has finished ``job`` yet."""
        if not job.finish():
            return False
        with self._jobs_lock:
            self._jobs.pop(job.id, None)
        log.debug("op=%s tier=%s status=%s duration_ms=%.1f",
                  job.request.op, job.tier.value, result.status.value, result.duration_ms)
        self._notifier.emit(result)
        return True

    def _on_worker_result(self, job: Job, result: ActionResult) -> None:
        if not self._finish(job, result):
            log.debug("late result discarded: op=%s status=%s", job.request.op, result.status.value)

    def _cancel_job(self, job: Job, reason: str) -> bool:
        job.token.cancel(reason)
        worker = self._workers.get(job.tier)
        if worker is not None:
            worker.queue.remove(job)
        return self._finish(job, ActionResult.for_request(job.request, Status.CANCELLED, reason))

    def _on_deadline(self, job: Job) -> None:
        """Watchdog callback at the job's deadline."""
        if job.state is JobState.DONE:
            return
        running = job.state is JobState.RUNNING
        job.token.cancel("timeout")
        worker = self._workers.get(job.tier)
        if worker is not None and not running:
            worker.queue.remove(job)
        where = "while running" if running else "while queued"
        result = ActionResult(
            id=job.id, status=Status.TIMEOUT, tier_used=job.tier if running else None,
            message=f"timed out after {job.request.timeout_s:g}s {where}",
        )
        if self._finish(job, result) and worker is not None:
            self._watchdog.schedule(time.monotonic() + self._stuck_grace_s, partial(worker.mark_stuck, job))
