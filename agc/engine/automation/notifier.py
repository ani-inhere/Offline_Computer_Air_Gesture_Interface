"""Result notifier: delivers ActionResults to the callback on its own thread.

Callbacks never run on a worker thread or on the decision loop, so a slow HUD
or IPC sink cannot stall automation or the camera.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from .models import ActionResult

log = logging.getLogger("agc.automation.notifier")

ResultCallback = Callable[[ActionResult], None]

_STOP = object()


class ResultNotifier:
    """Unbounded hand-off queue plus one delivery thread."""

    def __init__(self, name: str = "agc-automation-notifier") -> None:
        self._queue: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._callback: ResultCallback | None = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    @property
    def thread_name(self) -> str:
        return self._thread.name

    def set_callback(self, callback: ResultCallback | None) -> None:
        """Replace the result callback (``None`` disables delivery)."""
        self._callback = callback

    def start(self) -> None:
        """Start delivering. Results emitted before start are delivered once it runs."""
        if not self._thread.is_alive():
            self._thread.start()

    def emit(self, result: ActionResult) -> None:
        """Queue a result for delivery. Never blocks."""
        self._queue.put(result)

    def stop(self, timeout: float = 1.0) -> bool:
        """Deliver everything already emitted, then stop. Returns True if the thread exited."""
        if not self._thread.is_alive():
            return True
        self._queue.put(_STOP)
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            callback = self._callback
            if callback is None:
                continue
            try:
                callback(item)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - a bad sink must not stop delivery
                log.exception("result callback failed")
