"""ActionDispatcher behaviour: non-blocking submit, priority, coalescing, timeouts, cancel, results."""

from __future__ import annotations

import gc
import threading
import time

from fakes import BLOCK_OP, FakeGuiAdapter, FakeOsAdapter
from helpers import make_request, wait_until

from agc.engine.automation import ActionType, Backend, Priority, Status, Tier, WorkerHealth


def test_submit_never_blocks_while_worker_is_stuck(make_dispatcher):
    adapter = FakeOsAdapter(delay_s=5.0)
    d = make_dispatcher(queue_size=8)
    d.register_backend(Tier.OS, adapter)
    d.start()
    d.submit(make_request("slow", timeout_s=30))
    assert adapter.started.wait(1)

    # Mix of plain, coalescing and overflowing submits while the worker sleeps 5 s.
    requests = [make_request(f"op{i}", timeout_s=30, dedupe_key="k" if i % 3 == 0 else None)
                for i in range(500)]
    d.submit(requests[0])  # warm-up
    durations = []
    gc.disable()
    try:
        for request in requests[1:]:
            t0 = time.perf_counter()
            d.submit(request)
            durations.append(time.perf_counter() - t0)
    finally:
        gc.enable()
    durations.sort()
    p99 = durations[int(len(durations) * 0.99)]
    print(f"\nsubmit latency: p50={durations[len(durations) // 2] * 1e6:.1f}us "
          f"p99={p99 * 1e6:.1f}us max={durations[-1] * 1e6:.1f}us")
    assert durations[-1] < 1e-3


def test_release_priority_jumps_the_queue(make_dispatcher, collector):
    adapter = FakeOsAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    ids = [d.submit(make_request(BLOCK_OP))]
    assert adapter.started.wait(1)
    ids += [d.submit(make_request("a")), d.submit(make_request("b"))]
    ids.append(d.submit(make_request("key_up", priority=Priority.RELEASE)))
    adapter.gate.set()
    results = collector.wait_all(ids)
    assert all(r.status is Status.OK for r in results)
    assert adapter.executed_ops == [BLOCK_OP, "key_up", "a", "b"]


def test_overflow_coalesces_and_drops_oldest(make_dispatcher, collector):
    adapter = FakeOsAdapter()
    d = make_dispatcher(queue_size=4)
    d.register_backend(Tier.OS, adapter)
    d.start()
    block = d.submit(make_request(BLOCK_OP))
    assert adapter.started.wait(1)

    volume = [d.submit(make_request("volume", dedupe_key="volume", args={"v": v})) for v in range(3)]
    for job_id in volume[:2]:
        result = collector.wait_for(job_id)
        assert result.status is Status.DROPPED and "coalesced" in result.message
    others = [d.submit(make_request(f"x{i}")) for i in range(4)]  # 5th entry pushes out the volume job
    dropped = collector.wait_for(volume[2])
    assert dropped.status is Status.DROPPED and "queue full" in dropped.message

    adapter.gate.set()
    assert all(r.status is Status.OK for r in collector.wait_all([block, *others]))
    assert adapter.executed_ops == [BLOCK_OP, "x0", "x1", "x2", "x3"]


def test_timeout_with_cooperative_adapter_then_worker_recovers(make_dispatcher, collector):
    adapter = FakeOsAdapter(delay_s=2.0)
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    t0 = time.monotonic()
    slow = d.submit(make_request("slow", timeout_s=0.2))
    result = collector.wait_for(slow)
    assert result.status is Status.TIMEOUT
    assert time.monotonic() - t0 < 1.0

    adapter.delay_s = 0
    nxt = d.submit(make_request("next"))
    assert collector.wait_for(nxt).status is Status.OK
    assert len(collector.for_id(slow)) == 1


def test_timeout_with_uncooperative_adapter_discards_late_result(make_dispatcher, collector):
    adapter = FakeOsAdapter(delay_s=0.6, honor_cancel=False)
    d = make_dispatcher(stuck_grace_s=5.0)
    d.register_backend(Tier.OS, adapter)
    d.start()
    t0 = time.monotonic()
    slow = d.submit(make_request("slow", timeout_s=0.2))
    assert collector.wait_for(slow).status is Status.TIMEOUT
    assert time.monotonic() - t0 < 0.5  # watchdog moved on; the adapter is still sleeping

    nxt = d.submit(make_request("next", timeout_s=5))
    assert collector.wait_for(nxt).status is Status.OK
    assert len(collector.for_id(slow)) == 1  # the adapter's late OK was discarded


def test_stuck_worker_fails_fast_then_recovers(make_dispatcher, collector):
    adapter = FakeOsAdapter(delay_s=1.0, honor_cancel=False)
    d = make_dispatcher(stuck_grace_s=0.1)
    d.register_backend(Tier.OS, adapter)
    d.start()
    worker = d.worker(Tier.OS)
    slow = d.submit(make_request("slow", timeout_s=0.1))
    assert collector.wait_for(slow).status is Status.TIMEOUT
    assert wait_until(lambda: worker.health is WorkerHealth.STUCK, timeout=1.0)

    refused = d.submit(make_request("x"))
    result = collector.wait_for(refused)
    assert result.status is Status.UNAVAILABLE and "stuck" in result.message

    assert wait_until(lambda: worker.health is WorkerHealth.RUNNING, timeout=2.0)
    adapter.delay_s = 0
    assert collector.wait_for(d.submit(make_request("y"))).status is Status.OK


def test_cancel_queued_job_never_runs(make_dispatcher, collector):
    adapter = FakeOsAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    d.submit(make_request(BLOCK_OP))
    assert adapter.started.wait(1)
    victim = d.submit(make_request("victim"))
    assert d.cancel(victim) is True
    assert d.cancel(victim) is False
    assert collector.wait_for(victim).status is Status.CANCELLED
    adapter.gate.set()
    assert collector.wait_for(d.submit(make_request("after"))).status is Status.OK
    assert "victim" not in adapter.executed_ops


def test_cancel_all_keeps_release_jobs(make_dispatcher, collector):
    adapter = FakeOsAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    block = d.submit(make_request(BLOCK_OP, timeout_s=10))
    assert adapter.started.wait(1)
    queued = [d.submit(make_request(f"q{i}")) for i in range(5)]
    release = d.submit(make_request("key_up", priority=Priority.RELEASE))

    assert d.cancel_all() == 6  # running block + 5 queued
    for job_id in [block, *queued]:
        assert collector.wait_for(job_id).status is Status.CANCELLED
    adapter.gate.set()
    assert collector.wait_for(release).status is Status.OK
    assert collector.wait_for(d.submit(make_request("after"))).status is Status.OK
    assert len(collector.for_id(block)) == 1
    assert adapter.executed_ops == [BLOCK_OP, "key_up", "after"]


def test_callbacks_run_on_notifier_thread(make_dispatcher, collector):
    adapter = FakeOsAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    ids = [d.submit(make_request(f"op{i}")) for i in range(5)]
    collector.wait_all(ids)
    worker_thread = d.worker(Tier.OS).thread_name
    assert set(collector.thread_names) == {"agc-automation-notifier"}
    assert worker_thread not in collector.thread_names
    assert threading.current_thread().name not in collector.thread_names


def test_failing_callback_does_not_stop_delivery(make_dispatcher, collector):
    d = make_dispatcher()
    d.register_backend(Tier.OS, FakeOsAdapter())
    calls = []

    def flaky(result):
        calls.append(result.id)
        if len(calls) == 1:
            raise RuntimeError("sink broke")
        collector(result)

    d.set_result_callback(flaky)
    d.start()
    first = d.submit(make_request("a"))
    second = d.submit(make_request("b"))
    assert collector.wait_for(second).status is Status.OK
    assert calls[0] == first


def test_unregistered_tier_reports_unavailable(make_dispatcher, collector):
    d = make_dispatcher()
    d.register_backend(Tier.OS, FakeOsAdapter())
    d.start()
    job_id = d.submit(make_request("browser_next_tab", type=ActionType.GUI_MACRO))
    result = collector.wait_for(job_id)
    assert result.status is Status.UNAVAILABLE and "gui" in result.message


def test_explicit_backend_overrides_action_type(make_dispatcher, collector):
    gui = FakeGuiAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.OS, FakeOsAdapter())
    d.register_backend(Tier.GUI, gui)
    d.start()
    job_id = d.submit(make_request("play_pause", type=ActionType.OS, backend=Backend.GUI))
    result = collector.wait_for(job_id)
    assert result.status is Status.OK and result.tier_used is Tier.GUI
    assert gui.executed_ops == ["play_pause"]


def test_every_job_gets_exactly_one_result_under_a_storm(make_dispatcher, collector):
    adapter = FakeOsAdapter(delay_s=0.001)
    d = make_dispatcher(queue_size=8)
    d.register_backend(Tier.OS, adapter)
    d.start()
    ids = []
    for i in range(300):
        ids.append(d.submit(make_request(f"op{i}", dedupe_key="vol" if i % 2 else None,
                                         priority=Priority.RELEASE if i % 50 == 0 else Priority.NORMAL)))
        if i % 37 == 1:  # never a release job (i % 50 == 0)
            d.cancel(ids[-1])
    collector.wait_all(ids, timeout=10)
    time.sleep(0.1)  # let any stray late result arrive
    for job_id in ids:
        assert len(collector.for_id(job_id)) == 1
    release_ids = ids[::50]
    assert all(collector.for_id(i)[0].status is Status.OK for i in release_ids)
    assert d.stats()["pending_jobs"] == 0


def test_shutdown_stops_threads_and_finishes_pending_jobs(make_dispatcher, collector):
    adapter = FakeOsAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    block = d.submit(make_request(BLOCK_OP, timeout_s=10))
    assert adapter.started.wait(1)
    queued = d.submit(make_request("queued"))
    release = d.submit(make_request("key_up", priority=Priority.RELEASE))

    t0 = time.monotonic()
    assert d.shutdown(timeout=3.0) is True
    assert time.monotonic() - t0 < 3.0
    assert collector.wait_for(block).status is Status.CANCELLED
    assert collector.wait_for(queued).status is Status.CANCELLED
    assert collector.wait_for(release).status is Status.OK  # release jobs still run on shutdown
    assert adapter.executed_ops == [BLOCK_OP, "key_up"]
    assert adapter.shutdown_called
    assert d.worker(Tier.OS).health is WorkerHealth.STOPPED
    assert not d._watchdog._thread.is_alive() and not d._notifier._thread.is_alive()

    late = d.submit(make_request("late"))  # ignored, must not raise
    assert isinstance(late, str)
