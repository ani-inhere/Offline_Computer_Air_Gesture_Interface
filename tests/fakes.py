"""Fake adapters for automation tests: configurable delay, failures, crashes and gating."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from agc.engine.automation.adapters.base import AppAdapter, GuiAdapter, OsAdapter
from agc.engine.automation.cancel import CancelToken
from agc.engine.automation.models import ActionRequest, ActionResult, Status

#: Requests with this op wait on the adapter's ``gate`` so a test can hold the worker busy.
BLOCK_OP = "block"


class FakeCrash(BaseException):
    """Escapes the worker's per-job ``except Exception``, simulating a worker-thread crash."""


@dataclass
class Call:
    op: str
    request_id: str
    thread_ident: int


class _FakeBehaviour:
    """Shared fake logic. Mixed in before the tier base class so its methods win."""

    def _configure(
        self,
        *,
        name: str = "fake",
        delay_s: float = 0.0,
        honor_cancel: bool = True,
        available: bool = True,
        raises: BaseException | None = None,
        raise_times: int | None = None,
    ) -> None:
        self.name = name
        self.delay_s = delay_s
        self.honor_cancel = honor_cancel
        self.available = available
        self.raises = raises
        self.raise_times = raise_times
        self.gate = threading.Event()
        self.started = threading.Event()
        self.calls: list[Call] = []
        self.shutdown_called = False
        self.thread_started_idents: list[int] = []

    @property
    def executed_ops(self) -> list[str]:
        return [call.op for call in self.calls]

    def handles(self, request: ActionRequest) -> bool:
        return True

    def is_available(self) -> tuple[bool, str]:
        return self.available, "" if self.available else f"{self.name} unavailable"

    def execute(self, request: ActionRequest, cancel_token: CancelToken) -> ActionResult:
        self.calls.append(Call(request.op, request.id, threading.get_ident()))
        self.started.set()
        if request.op == BLOCK_OP:
            deadline = time.monotonic() + 10
            while not self.gate.wait(0.005) and time.monotonic() < deadline:
                cancel_token.raise_if_cancelled()
        if self.raises is not None and (self.raise_times is None or self.raise_times > 0):
            if self.raise_times is not None:
                self.raise_times -= 1
            raise self.raises
        if self.delay_s:
            if self.honor_cancel:
                cancel_token.wait(self.delay_s)
                cancel_token.raise_if_cancelled()
            else:
                time.sleep(self.delay_s)
        return ActionResult.for_request(request, Status.OK, f"{self.name} ran {request.op}")

    def shutdown(self) -> None:
        self.shutdown_called = True

    def thread_started(self) -> None:
        self.thread_started_idents.append(threading.get_ident())


class FakeOsAdapter(_FakeBehaviour, OsAdapter):
    def __init__(self, **kwargs) -> None:
        self._configure(**{"name": "fake-os", **kwargs})


class FakeAppAdapter(_FakeBehaviour, AppAdapter):
    app = "fake"

    def __init__(self, **kwargs) -> None:
        self._configure(**{"name": "fake-app", **kwargs})


class FakeGuiAdapter(_FakeBehaviour, GuiAdapter):
    def __init__(self, **kwargs) -> None:
        self._configure(**{"name": "fake-gui", **kwargs})
        self.release_all_calls = 0

    def release_all(self) -> None:
        self.release_all_calls += 1
