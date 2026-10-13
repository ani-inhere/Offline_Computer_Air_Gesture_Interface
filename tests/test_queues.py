"""JobQueue: release channel first, dedupe coalescing, drop-oldest overflow."""

from __future__ import annotations

from helpers import make_request

from agc.engine.automation import Priority, Tier
from agc.engine.automation.jobs import Job
from agc.engine.automation.queues import JobQueue


def job(op: str = "x", **kwargs) -> Job:
    return Job(make_request(op, **kwargs), Tier.OS)


def test_release_jobs_come_out_first():
    q = JobQueue(maxsize=4)
    a, b, rel = job("a"), job("b"), job("rel", priority=Priority.RELEASE)
    for j in (a, b, rel):
        assert q.put(j) == []
    assert [q.get(0).request.op for _ in range(3)] == ["rel", "a", "b"]
    assert q.get(0) is None


def test_dedupe_key_replaces_in_place():
    q = JobQueue(maxsize=4)
    first, other, second = job("vol", dedupe_key="v"), job("x"), job("vol", dedupe_key="v")
    q.put(first)
    q.put(other)
    displaced = q.put(second)
    assert [d for d, _ in displaced] == [first]
    assert "coalesced" in displaced[0][1]
    assert [q.get(0) for _ in range(2)] == [second, other]


def test_overflow_drops_oldest_normal_job():
    q = JobQueue(maxsize=2)
    a, b, c = job("a"), job("b"), job("c")
    q.put(a)
    q.put(b)
    displaced = q.put(c)
    assert [d for d, _ in displaced] == [a]
    assert q.depth() == (0, 2)


def test_release_channel_is_never_dropped():
    q = JobQueue(maxsize=1)
    releases = [job(f"r{i}", priority=Priority.RELEASE) for i in range(50)]
    for r in releases:
        assert q.put(r) == []
    assert q.depth() == (50, 0)


def test_remove_and_drain():
    q = JobQueue(maxsize=4)
    a, b, rel = job("a"), job("b"), job("rel", priority=Priority.RELEASE)
    for j in (a, b, rel):
        q.put(j)
    assert q.remove(a) is True
    assert q.remove(a) is False
    assert q.drain_normal() == [b]
    assert q.drain_release() == [rel]
    assert q.depth() == (0, 0)
