"""Cooperative cancellation shared between the dispatcher, watchdog and adapters.

Threads are never killed. Adapters check the token between steps (or sleep on it
with :meth:`CancelToken.wait`) and stop early once it is set.
"""

from __future__ import annotations

import threading


class CancelledError(Exception):
    """Raised by :meth:`CancelToken.raise_if_cancelled`; workers turn it into a result."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    """Thread-safe, one-way cancel flag with a reason (``"cancelled"``, ``"timeout"``, ...)."""

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        """Set the flag. The first reason wins; later calls are no-ops."""
        if self._reason is None:
            self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` has been called."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Why the token was cancelled, or ``None``."""
        return self._reason

    def raise_if_cancelled(self) -> None:
        """Raise :class:`CancelledError` if cancelled. Call between adapter steps."""
        if self._event.is_set():
            raise CancelledError(self._reason or "cancelled")

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds, waking early on cancel. Returns True if cancelled."""
        return self._event.wait(timeout)
