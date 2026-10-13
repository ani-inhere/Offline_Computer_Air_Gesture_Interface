"""Shared fixtures for automation tests."""

from __future__ import annotations

from typing import Any

import pytest
from helpers import ResultCollector

from agc.engine.automation import ActionDispatcher


@pytest.fixture
def collector() -> ResultCollector:
    return ResultCollector()


@pytest.fixture
def make_dispatcher(collector: ResultCollector):
    """Factory for dispatchers wired to ``collector``; all are shut down at teardown."""
    created: list[ActionDispatcher] = []

    def factory(**kwargs: Any) -> ActionDispatcher:
        kwargs.setdefault("worker_options", {"poll_s": 0.02, "backoff_initial_s": 0.01})
        dispatcher = ActionDispatcher(**kwargs)
        dispatcher.set_result_callback(collector)
        created.append(dispatcher)
        return dispatcher

    yield factory
    for dispatcher in created:
        dispatcher.shutdown(timeout=1.0)
