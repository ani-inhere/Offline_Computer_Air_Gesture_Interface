"""Automation backend: gesture actions executed through OS, app-API and GUI tiers.

The decision loop only calls :meth:`ActionDispatcher.submit`; everything else
runs on worker threads. See docs/agc-automation-backend-flow-and-prompts.md.
"""

from .cancel import CancelledError, CancelToken
from .dispatcher import ActionDispatcher
from .models import ActionRequest, ActionResult, ActionType, Backend, Priority, Status, Tier
from .workers import WorkerHealth

__all__ = [
    "ActionDispatcher",
    "ActionRequest",
    "ActionResult",
    "ActionType",
    "Backend",
    "CancelToken",
    "CancelledError",
    "Priority",
    "Status",
    "Tier",
    "WorkerHealth",
]
