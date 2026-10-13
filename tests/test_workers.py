"""Worker threads: crash/restart, adapter failures, COM apartment ownership, gui_lock."""

from __future__ import annotations

import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest
from fakes import FakeAppAdapter, FakeCrash, FakeGuiAdapter, FakeOsAdapter
from helpers import make_request, wait_until

from agc.engine.automation import ActionType, Status, Tier, WorkerHealth
from agc.engine.automation.workers import gui_lock


def test_adapter_exception_becomes_failed_result(make_dispatcher, collector):
    adapter = FakeOsAdapter(raises=RuntimeError("device gone"), raise_times=1)
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    result = collector.wait_for(d.submit(make_request("mute")))
    assert result.status is Status.FAILED and "device gone" in result.message
    assert result.tier_used is Tier.OS
    assert collector.wait_for(d.submit(make_request("mute"))).status is Status.OK
    assert d.worker(Tier.OS).restart_count == 0


def test_worker_crash_restarts_with_backoff(make_dispatcher, collector):
    adapter = FakeOsAdapter(raises=FakeCrash("boom"), raise_times=1)
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    worker = d.worker(Tier.OS)
    crashed = collector.wait_for(d.submit(make_request("crash")))
    assert crashed.status is Status.FAILED and "worker crashed" in crashed.message

    assert collector.wait_for(d.submit(make_request("after"))).status is Status.OK
    assert worker.restart_count == 1
    assert worker.health is WorkerHealth.RUNNING
    # thread_started ran twice on the same thread: once at start, once after the restart
    assert len(adapter.thread_started_idents) == 2
    assert len(set(adapter.thread_started_idents)) == 1


def test_adapter_without_matching_op_reports_unavailable(make_dispatcher, collector):
    from agc.engine.automation.adapters.base import OsAdapter

    class MediaOnly(FakeOsAdapter):
        ops = frozenset({"play_pause"})
        handles = OsAdapter.handles

    d = make_dispatcher()
    d.register_backend(Tier.OS, MediaOnly())
    d.start()
    result = collector.wait_for(d.submit(make_request("brightness_up")))
    assert result.status is Status.UNAVAILABLE and "brightness_up" in result.message
    assert collector.wait_for(d.submit(make_request("play_pause"))).status is Status.OK


def test_register_rejects_adapter_for_wrong_tier(make_dispatcher):
    d = make_dispatcher()
    with pytest.raises(ValueError):
        d.register_backend(Tier.APP_API, FakeOsAdapter())


@pytest.fixture
def fake_pythoncom(monkeypatch):
    events: list[tuple[str, int]] = []
    module = types.ModuleType("pythoncom")
    module.CoInitialize = lambda: events.append(("init", threading.get_ident()))
    module.CoUninitialize = lambda: events.append(("uninit", threading.get_ident()))
    monkeypatch.setitem(sys.modules, "pythoncom", module)
    return events


def test_com_worker_owns_apartment_on_one_thread(make_dispatcher, collector, fake_pythoncom):
    adapter = FakeAppAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.APP_API, adapter)
    d.start()
    ids = [d.submit(make_request(f"op{i}", type=ActionType.APP_API, app="fake")) for i in range(3)]
    assert all(r.status is Status.OK for r in collector.wait_all(ids))
    d.shutdown(timeout=2)

    com_thread = d.worker(Tier.APP_API)
    exec_threads = {call.thread_ident for call in adapter.calls}
    assert len(exec_threads) == 1
    (thread_ident,) = exec_threads
    assert thread_ident != threading.get_ident()
    assert fake_pythoncom == [("init", thread_ident), ("uninit", thread_ident)]
    assert com_thread.health is WorkerHealth.STOPPED


@pytest.mark.skipif(sys.platform != "win32", reason="real pythoncom is Windows-only")
def test_com_worker_with_real_pythoncom(make_dispatcher, collector):
    pytest.importorskip("pythoncom")
    d = make_dispatcher()
    d.register_backend(Tier.APP_API, FakeAppAdapter())
    d.start()
    result = collector.wait_for(d.submit(make_request("op", type=ActionType.APP_API, app="fake")))
    assert result.status is Status.OK
    assert d.shutdown(timeout=2)


def test_gui_worker_holds_gui_lock_during_jobs(make_dispatcher, collector):
    seen = []

    class LockProbe(FakeGuiAdapter):
        def execute(self, request, cancel_token):
            # RLock has no public "locked" query; a non-blocking acquire from another thread tells us.
            probe = threading.Thread(target=lambda: seen.append(gui_lock.acquire(blocking=False)))
            probe.start()
            probe.join()
            return super().execute(request, cancel_token)

    d = make_dispatcher()
    d.register_backend(Tier.GUI, LockProbe())
    d.start()
    assert collector.wait_for(d.submit(make_request("hotkey", type=ActionType.GUI_MACRO))).ok
    assert seen == [False]
    assert gui_lock.acquire(blocking=False)
    gui_lock.release()


def test_stats_report_queue_depth_and_health(make_dispatcher):
    adapter = FakeOsAdapter()
    d = make_dispatcher()
    d.register_backend(Tier.OS, adapter)
    d.start()
    d.submit(make_request("block"))
    assert adapter.started.wait(1)
    d.submit(make_request("queued"))
    assert wait_until(lambda: d.stats()["workers"]["os"]["queue_normal"] == 1)
    stats = d.stats()["workers"]["os"]
    assert stats["health"] == "running" and stats["current_op"] == "block"
    adapter.gate.set()


def test_package_import_does_not_load_platform_modules():
    code = (
        "import sys, agc.engine.automation as a; "
        "bad = [m for m in ('pythoncom', 'win32com', 'pywintypes', 'pyautogui', 'cv2') if m in sys.modules]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=repo_root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
