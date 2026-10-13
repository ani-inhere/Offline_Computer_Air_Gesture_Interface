"""Pydantic schemas for the automation backend: requests in, results out."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionType(str, Enum):
    """Kind of action a request describes (extends PRD CTX-5)."""

    OS = "os"
    APP_API = "app_api"
    GUI_MACRO = "gui_macro"


class Backend(str, Enum):
    """Tier a profile asks for; ``auto`` lets the dispatcher/router choose."""

    AUTO = "auto"
    OS = "os"
    APP_API = "app_api"
    GUI = "gui"


class Tier(str, Enum):
    """Execution tier, one worker thread per tier."""

    OS = "os"
    APP_API = "app_api"
    GUI = "gui"


class Priority(str, Enum):
    """``release`` requests (key-up, release_all) skip the normal queue and are never dropped."""

    NORMAL = "normal"
    RELEASE = "release"


class Status(str, Enum):
    """Terminal outcome of a job. Every submitted job ends with exactly one of these."""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    DROPPED = "dropped"


def _new_id() -> str:
    return uuid.uuid4().hex


class ActionRequest(BaseModel):
    """One automation command produced by the mapper.

    ``timeout_s`` is measured from submission, so it covers time spent queued as
    well as execution: a gesture that cannot act within its window is stale and
    should not fire late.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=_new_id)
    type: ActionType
    app: str | None = None
    op: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    backend: Backend = Backend.AUTO
    timeout_s: float = Field(default=5.0, gt=0, le=600)
    gui_fallback: str | None = None
    priority: Priority = Priority.NORMAL
    dedupe_key: str | None = None
    created_ts: float = Field(default_factory=time.monotonic)


class ActionResult(BaseModel):
    """Outcome of one request, delivered to result callbacks (and from there to the HUD)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: Status
    tier_used: Tier | None = None
    fallback_used: bool = False
    duration_ms: float = 0.0
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the action completed successfully."""
        return self.status is Status.OK

    @classmethod
    def for_request(
        cls,
        request: ActionRequest,
        status: Status,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> ActionResult:
        """Build a result carrying the request's id."""
        return cls(id=request.id, status=status, message=message, data=data or {})
