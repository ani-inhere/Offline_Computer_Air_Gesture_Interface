"""Adapter contracts for the three automation tiers.

An adapter runs only on its tier's worker thread. ``execute`` should return an
:class:`ActionResult` rather than raise; any exception that does escape is
turned into a FAILED result by the worker. Long operations must check the
cancel token between steps.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..cancel import CancelToken
from ..models import ActionRequest, ActionResult, Tier


class BaseAdapter(ABC):
    """Common interface for every tier."""

    tier: ClassVar[Tier]
    name: str = "adapter"

    def handles(self, request: ActionRequest) -> bool:
        """True if this adapter can execute ``request``. Workers pick the first adapter that does."""
        return True

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """``(available, reason)``. Must be cheap-ish and must not raise."""

    @abstractmethod
    def execute(self, request: ActionRequest, cancel_token: CancelToken) -> ActionResult:
        """Run ``request`` on the calling (worker) thread and return its result."""

    def shutdown(self) -> None:
        """Release resources. Called on the worker thread as it stops."""

    def thread_started(self) -> None:
        """Hook run on the worker thread when it (re)starts, after e.g. CoInitialize."""

    def thread_stopped(self) -> None:
        """Hook run on the worker thread before it exits or restarts after a crash."""


class OsAdapter(BaseAdapter):
    """Tier 1: native OS controls (media keys, volume, brightness)."""

    tier = Tier.OS
    ops: ClassVar[frozenset[str]] = frozenset()

    def handles(self, request: ActionRequest) -> bool:
        return request.op in self.ops


class AppAdapter(BaseAdapter):
    """Tier 2: an application's own API (e.g. Word over COM). One instance per app."""

    tier = Tier.APP_API
    app: ClassVar[str] = ""

    def handles(self, request: ActionRequest) -> bool:
        return request.app == self.app


class GuiAdapter(BaseAdapter):
    """Tier 3: synthetic keyboard/mouse input (PyAutoGUI)."""

    tier = Tier.GUI

    def release_all(self) -> None:
        """Release every key/button this adapter holds. Must be safe from any thread."""
