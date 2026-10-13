"""Interfaces the automation backend needs from engine parts that do not exist yet.

PRD B12.1 (ForegroundAdapter), B10.2 (Pointer mode) and C1 (IPC to the HUD)
describe these; until they are built, the automation layer depends only on
these Protocols so the real implementations can plug in later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import ActionResult


@dataclass(frozen=True)
class ForegroundInfo:
    """Identity of the foreground window (PRD CTX-4: identity and title only, never contents)."""

    process: str
    title: str
    pid: int | None = None
    elevated: bool | None = None


class ForegroundProbe(Protocol):
    """Reads the current foreground app (PRD B7.1, B12.1 ForegroundAdapter)."""

    def current(self) -> ForegroundInfo | None:
        """Foreground window info, or None if unknown."""
        ...


class CursorOwner(Protocol):
    """Arbitrates the cursor between Pointer mode and GUI macros (Phase 3)."""

    def acquire(self, owner: str, timeout: float) -> bool:
        """Take the cursor for ``owner``; False if not granted within ``timeout``."""
        ...

    def release(self, owner: str) -> None:
        """Give the cursor back."""
        ...

    @property
    def pointer_active(self) -> bool:
        """True while the air mouse (Pointer mode) is driving the cursor."""
        ...


class ResultSink(Protocol):
    """Receives results for the HUD (IPC layer, PRD C1/HUD-3 action toasts)."""

    def __call__(self, result: ActionResult) -> None: ...
