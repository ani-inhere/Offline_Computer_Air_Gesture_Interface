"""Cursor arbitration between Pointer mode (air mouse) and GUI macros.

Pointer mode is the user's live hand and always wins: it flips a flag with
:meth:`CursorArbiter.set_pointer_active`, which never waits, so the decision
loop can call it every frame. GUI automation must :meth:`acquire` the cursor;
that waits (bounded) while Pointer mode is active and fails if it stays active.
Macros also poll :attr:`pointer_active` between steps and abort if the user
takes the cursor back mid-macro.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


class CursorBusyError(RuntimeError):
    """The cursor could not be acquired (Pointer mode active or another owner)."""


class CursorArbiter:
    """Implements :class:`~agc.engine.automation.interfaces.CursorOwner`."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._pointer = False
        self._owner: str | None = None

    @property
    def pointer_active(self) -> bool:
        return self._pointer

    @property
    def owner(self) -> str | None:
        return self._owner

    def set_pointer_active(self, active: bool) -> None:
        """Called by Pointer mode on enter/exit. Never blocks beyond a tiny lock."""
        with self._cond:
            self._pointer = active
            self._cond.notify_all()

    def acquire(self, owner: str, timeout: float) -> bool:
        """Take the cursor for ``owner``, waiting up to ``timeout`` s for Pointer mode/other owners."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pointer or (self._owner is not None and self._owner != owner):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._owner = owner
            return True

    def release(self, owner: str) -> None:
        """Give the cursor back (no-op if ``owner`` does not hold it)."""
        with self._cond:
            if self._owner == owner:
                self._owner = None
                self._cond.notify_all()

    @contextmanager
    def hold(self, owner: str, timeout: float) -> Iterator[None]:
        """Context manager around :meth:`acquire`/:meth:`release`; raises :class:`CursorBusyError`."""
        if not self.acquire(owner, timeout):
            reason = "Pointer mode is using the cursor" if self._pointer else f"cursor held by {self._owner}"
            raise CursorBusyError(reason)
        try:
            yield
        finally:
            self.release(owner)
