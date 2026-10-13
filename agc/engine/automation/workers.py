"""Tier worker threads: one thread per tier, jobs run serially on it.

* OsWorker  - Tier 1 OS adapters.
* ComWorker - Tier 2 app adapters. Owns the COM apartment: CoInitialize on
  thread start, CoUninitialize on exit. COM objects never leave this thread.
* GuiWorker - Tier 3 GUI automation, every job under the global ``gui_lock``.

A worker survives adapter exceptions (FAILED result) and restarts itself with
exponential backoff if its loop crashes. It never blocks the decision loop:
the dispatcher hands it jobs through a :class:`JobQueue`.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, ClassVar

from .adapters.base import BaseAdapter
from .cancel import CancelledError
from .jobs import Job
from .models import ActionRequest, ActionResult, Status, Tier
from .queues import JobQueue

log = logging.getLogger("agc.automation.workers")

#: Serializes every PyAutoGUI call (PyAutoGUI is not thread-safe). Release paths
#: (key-up ledger) deliberately do not take it, so a stuck macro cannot block them.
gui_lock = threading.RLock()

CompleteFn = Callable[[Job, ActionResult], Any]


class WorkerHealth(str, Enum):
    """Health reported to diagnostics and the dispatcher."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    STUCK = "stuck"


class BaseWorker:
    """Thread that runs jobs from one :class:`JobQueue` through its adapters."""

    tier: ClassVar[Tier]
    thread_name: ClassVar[str] = "agc-worker"

    def __init__(
        self,
        queue: JobQueue,
        complete: CompleteFn,
        *,
        poll_s: float = 0.1,
        backoff_initial_s: float = 0.05,
        backoff_max_s: float = 2.0,
    ) -> None:
        self.queue = queue
        self._complete = complete
        self._poll_s = poll_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._backoff_s = backoff_initial_s
        self._adapters: list[BaseAdapter] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._health = WorkerHealth.STOPPED
        self._stuck_job: Job | None = None
        self.current_job: Job | None = None
        self.restart_count = 0

    # ----------------------------------------------------------------- public

    @property
    def adapters(self) -> tuple[BaseAdapter, ...]:
        return tuple(self._adapters)

    @property
    def health(self) -> WorkerHealth:
        if self._stuck_job is not None and self._stuck_job is self.current_job:
            return WorkerHealth.STUCK
        return self._health

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident if self._thread else None

    def add_adapter(self, adapter: BaseAdapter) -> None:
        """Register an adapter. Adapters are tried in registration order."""
        if adapter.tier is not self.tier:
            raise ValueError(f"{adapter.name} is a {adapter.tier.value} adapter, worker is {self.tier.value}")
        self._adapters.append(adapter)

    def start(self) -> None:
        """Start the thread (no-op if already running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._health = WorkerHealth.STARTING
        self._thread = threading.Thread(target=self._main, name=self.thread_name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to exit after its current job and any queued release jobs."""
        self._stop.set()
        self.queue.wake()

    def join(self, timeout: float) -> bool:
        """Wait for the thread to exit. Returns True if it has."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def mark_stuck(self, job: Job) -> None:
        """Called by the watchdog when ``job`` overran its timeout plus grace and is still running."""
        if self.current_job is job:
            self._stuck_job = job
            log.warning("%s stuck on op=%s", self.thread_name, job.request.op)

    # ------------------------------------------------------------------ hooks

    def on_thread_start(self) -> None:
        """Per-thread setup, run on the worker thread (e.g. CoInitialize)."""

    def on_thread_stop(self) -> None:
        """Per-thread teardown, run on the worker thread (e.g. CoUninitialize)."""

    def invoke(self, adapter: BaseAdapter, job: Job) -> ActionResult:
        """Run one job on ``adapter``. Subclasses wrap this (e.g. in ``gui_lock``)."""
        return adapter.execute(job.request, job.token)

    # --------------------------------------------------------------- internals

    def _main(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._enter_thread()
                    self._health = WorkerHealth.RUNNING
                    self._loop()
                    return
                except BaseException as exc:  # noqa: BLE001 - any escape is a crash; restart
                    self._on_crash(exc)
                finally:
                    self._exit_thread()
                if self._stop.wait(self._backoff_s):
                    return
                self._backoff_s = min(self._backoff_s * 2, self._backoff_max_s)
        finally:
            self._health = WorkerHealth.STOPPED

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.queue.get(self._poll_s)
            if job is not None:
                self._run(job)
        # Stopping: release jobs are never dropped, so run whatever is left.
        for job in self.queue.drain_release():
            self._run(job)

    def _run(self, job: Job) -> None:
        if not job.start():
            return  # already finished: timed out, cancelled or dropped while queued
        self.current_job = job
        request = job.request
        t0 = time.perf_counter()
        adapter = self._pick(request)
        if adapter is None:
            result = ActionResult.for_request(
                request, Status.UNAVAILABLE, f"no {self.tier.value} adapter handles op {request.op!r}"
            )
        else:
            try:
                result = self.invoke(adapter, job)
            except CancelledError as exc:
                status = Status.TIMEOUT if exc.reason == "timeout" else Status.CANCELLED
                result = ActionResult.for_request(request, status, exc.reason)
            except Exception as exc:  # noqa: BLE001 - adapters must not take the worker down
                log.warning("%s: op=%s raised %s", adapter.name, request.op, type(exc).__name__)
                result = ActionResult.for_request(request, Status.FAILED, f"{type(exc).__name__}: {exc}")
        result = result.model_copy(
            update={"id": job.id, "tier_used": self.tier, "duration_ms": (time.perf_counter() - t0) * 1000}
        )
        self.current_job = None
        self._stuck_job = None
        self._backoff_s = self._backoff_initial_s
        self._complete(job, result)

    def _pick(self, request: ActionRequest) -> BaseAdapter | None:
        for adapter in self._adapters:
            if adapter.handles(request):
                return adapter
        return None

    def _on_crash(self, exc: BaseException) -> None:
        self.restart_count += 1
        self._health = WorkerHealth.RESTARTING
        log.error("%s crashed (%s); restarting in %.2fs", self.thread_name, type(exc).__name__, self._backoff_s,
                  exc_info=exc)
        job, self.current_job, self._stuck_job = self.current_job, None, None
        if job is not None:
            self._complete(job, ActionResult(
                id=job.id, status=Status.FAILED, tier_used=self.tier,
                message=f"worker crashed: {type(exc).__name__}",
            ))

    def _enter_thread(self) -> None:
        self.on_thread_start()
        for adapter in self._adapters:
            adapter.thread_started()

    def _exit_thread(self) -> None:
        final = self._stop.is_set()
        for adapter in self._adapters:
            try:
                adapter.thread_stopped()
                if final:
                    adapter.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("%s: adapter %s teardown failed", self.thread_name, adapter.name)
        try:
            self.on_thread_stop()
        except Exception:  # noqa: BLE001
            log.exception("%s: thread teardown failed", self.thread_name)


class OsWorker(BaseWorker):
    """Tier 1 worker for native OS adapters."""

    tier = Tier.OS
    thread_name = "agc-os-worker"


class ComWorker(BaseWorker):
    """Tier 2 worker. The only thread that may create or touch COM objects.

    ``pythoncom`` is imported lazily, so this module imports cleanly on
    non-Windows systems; there the worker simply runs without COM and the app
    adapters report themselves unavailable.
    """

    tier = Tier.APP_API
    thread_name = "agc-com-worker"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pythoncom: Any = None

    def on_thread_start(self) -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]
        except ImportError:
            log.debug("pythoncom not available; COM worker running without COM")
            return
        pythoncom.CoInitialize()  # single-threaded apartment
        self._pythoncom = pythoncom

    def on_thread_stop(self) -> None:
        if self._pythoncom is not None:
            self._pythoncom.CoUninitialize()
            self._pythoncom = None


class GuiWorker(BaseWorker):
    """Tier 3 worker. Every job holds ``gui_lock`` so input injection never interleaves."""

    tier = Tier.GUI
    thread_name = "agc-gui-worker"

    def invoke(self, adapter: BaseAdapter, job: Job) -> ActionResult:
        with gui_lock:
            return adapter.execute(job.request, job.token)
