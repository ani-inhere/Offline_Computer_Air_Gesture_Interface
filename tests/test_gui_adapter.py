"""GUI adapter: primitives, input ledger, release_all, cursor arbitration, ACT-7, dispatcher wiring."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fake_gui import RecordingBackend, ScriptedProbe, window
from helpers import make_request

from agc.engine.automation import ActionType, CancelToken, Priority, Status, Tier
from agc.engine.automation.adapters.gui_pyautogui import GuiAutomationAdapter, GuiConfig
from agc.engine.automation.cursor import CursorArbiter
from agc.engine.automation.macros import MacroLibrary


@pytest.fixture
def snippets_file(tmp_path: Path) -> Path:
    path = tmp_path / "snippets.yaml"
    path.write_text("snippets:\n  sig: \"Regards\"\n", encoding="utf-8")
    return path


@pytest.fixture
def make_gui(tmp_path, snippets_file):
    def factory(*, backend=None, probe=None, cursor=None, macros_yaml: str | None = None, **config):
        library = MacroLibrary()
        if macros_yaml:
            library.add_yaml(macros_yaml)
        settings = {"snippets_path": snippets_file, "allowed_output_dir": tmp_path / "out",
                    "cursor_wait_s": 0.05, **config}
        backend = backend or RecordingBackend()
        adapter = GuiAutomationAdapter(GuiConfig(**settings), backend=backend,
                                       probe=probe or ScriptedProbe(window("notepad.exe")),
                                       cursor=cursor or CursorArbiter(), macros=library, self_elevated=False)
        return adapter, backend

    return factory


def run(adapter, op, token=None, **args):
    request = make_request(op, type=ActionType.GUI_MACRO, args=args)
    result = adapter.execute(request, token or CancelToken())
    assert result.id == request.id
    return result


# -------------------------------------------------------------- primitives


def test_hotkey_and_press(make_gui):
    adapter, backend = make_gui()
    assert run(adapter, "hotkey", keys=["Ctrl", "s"]).ok
    assert run(adapter, "press", key="enter", presses=2).ok
    assert backend.calls == [("hotkey", ("ctrl", "s")), ("press", "enter", 2)]


@pytest.mark.parametrize("op, args, message", [
    ("hotkey", {"keys": ["ctrl", "nuke"]}, "unknown key 'nuke'"),
    ("hotkey", {"keys": []}, "list of 1-5"),
    ("press", {"key": "a", "presses": 99}, "presses must be"),
    ("click_at", {"x": "1", "y": 2}, "integer screen coordinates"),
    ("click_at", {"x": 1, "y": 2, "button": "side"}, "button must be"),
    ("scroll", {"clicks": 0}, "non-zero"),
    ("type_text", {}, "exactly one of"),
    ("type_text", {"snippet_id": "nope"}, "no snippet named"),
    ("run_macro", {"macro": "ghost"}, "unknown macro 'ghost'"),
    ("teleport", {}, "unknown GUI op"),
])
def test_invalid_requests_fail_cleanly(make_gui, op, args, message):
    adapter, backend = make_gui()
    result = run(adapter, op, **args)
    assert result.status is Status.FAILED and message in result.message
    assert backend.calls == []


def test_short_ascii_text_is_typed_long_text_is_pasted(make_gui):
    adapter, backend = make_gui(paste_threshold=10)
    run(adapter, "type_text", text="short")
    run(adapter, "type_text", text="this is longer than ten")
    run(adapter, "type_text", snippet_id="sig")
    assert backend.calls == [("write", "short"), ("paste", "this is longer than ten"), ("write", "Regards")]


def test_non_ascii_text_is_pasted_or_refused(make_gui):
    adapter, backend = make_gui()
    assert run(adapter, "type_text", text="café").ok
    assert backend.calls == [("paste", "café")]
    adapter2, backend2 = make_gui(backend=RecordingBackend(paste_works=False))
    result = run(adapter2, "type_text", text="café")
    assert result.status is Status.FAILED and "cannot be typed" in result.message
    assert run(adapter2, "type_text", text="x" * 50).ok  # long ASCII falls back to typing
    assert backend2.calls == [("write", "x" * 50)]


def test_click_restores_cursor_and_respects_pointer_mode(make_gui):
    cursor = CursorArbiter()
    adapter, backend = make_gui(cursor=cursor)
    assert run(adapter, "click_at", x=500, y=400, button="right").ok
    assert backend.calls == [("click", 500, 400, "right", 1), ("move", 100, 100)]

    cursor.set_pointer_active(True)
    result = run(adapter, "click_at", x=1, y=1)
    assert result.status is Status.FAILED and "Pointer mode" in result.message
    assert len(backend.calls) == 2


def test_scroll_with_and_without_position(make_gui):
    adapter, backend = make_gui()
    run(adapter, "scroll", clicks=-3)
    run(adapter, "scroll", clicks=2, x=10, y=20)
    assert backend.calls == [("scroll", -3, None, None), ("scroll", 2, 10, 20), ("move", 100, 100)]


# ------------------------------------------------------------------ ledger


def test_key_hold_is_tracked_and_released(make_gui):
    adapter, backend = make_gui()
    run(adapter, "key_hold", key="alt")
    run(adapter, "key_hold", key="shift")
    assert adapter.ledger.held == (["alt", "shift"], [])
    run(adapter, "key_release", key="shift")
    assert adapter.ledger.held == (["alt"], [])
    result = run(adapter, "release_all")
    assert result.ok and result.data["keys"] == ["alt"]
    assert backend.calls[-1] == ("key_up", "alt") and adapter.ledger.held == ([], [])


def test_release_all_from_another_thread_while_gui_lock_is_held(make_gui):
    from agc.engine.automation.workers import gui_lock

    adapter, backend = make_gui()
    run(adapter, "key_hold", key="alt")
    adapter.ledger.hold_button("left")
    done = threading.Event()
    with gui_lock:  # simulate a macro holding the GUI lock on the worker
        threading.Thread(target=lambda: (adapter.release_all(), done.set())).start()
        assert done.wait(1), "release_all must not wait for gui_lock"
    assert ("key_up", "alt") in backend.calls and ("mouse_up", "left") in backend.calls
    assert adapter.ledger.held == ([], [])


def test_release_all_keeps_going_after_a_failure(make_gui):
    adapter, backend = make_gui(backend=RecordingBackend(fail_on="key_up"))
    adapter.ledger.hold_key("alt")
    adapter.ledger.hold_button("left")
    adapter.release_all()
    assert ("mouse_up", "left") in backend.calls


def test_shutdown_releases_everything(make_gui):
    adapter, backend = make_gui()
    run(adapter, "key_hold", key="ctrl")
    adapter.shutdown()
    assert backend.calls[-1] == ("key_up", "ctrl")


# ------------------------------------------------------------ ACT-7 elevated


def test_elevated_foreground_blocks_input_but_not_releases(make_gui):
    adapter, backend = make_gui(probe=ScriptedProbe(window("Taskmgr.exe", elevated=True)))
    adapter.ledger.hold_key("alt")
    blocked = run(adapter, "hotkey", keys=["ctrl", "c"])
    assert blocked.status is Status.FAILED
    assert blocked.message.startswith("Can't control Taskmgr.exe: it runs as administrator")
    assert blocked.data == {"blocked_by": "elevated_window", "process": "Taskmgr.exe"}
    assert run(adapter, "release_all").ok
    assert backend.calls == [("key_up", "alt")]


def test_elevated_agc_may_control_elevated_windows(make_gui):
    adapter, backend = make_gui(probe=ScriptedProbe(window(elevated=True)))
    adapter.self_elevated = True
    assert run(adapter, "hotkey", keys=["a"]).ok


# ------------------------------------------------------------------ macros


MACRO = "name: save_it\nrequires_foreground: {process: [notepad.exe]}\nsteps: [{hotkey: [ctrl, s]}, {wait: 0.01}]"


def test_run_macro_by_args_or_by_name(make_gui):
    adapter, backend = make_gui(macros_yaml=MACRO)
    result = run(adapter, "run_macro", macro="save_it")
    assert result.ok and result.data == {"macro": "save_it", "steps_run": 2}
    assert run(adapter, "save_it").ok
    assert backend.calls == [("hotkey", ("ctrl", "s"))] * 2
    assert adapter.handles(make_request("save_it", type=ActionType.GUI_MACRO))
    assert not adapter.handles(make_request("ghost", type=ActionType.GUI_MACRO))


def test_failed_macro_reports_step(make_gui):
    adapter, _ = make_gui(macros_yaml=MACRO, probe=ScriptedProbe(window("chrome.exe")))
    result = run(adapter, "save_it")
    assert result.status is Status.FAILED and "macro needs notepad.exe" in result.message
    assert result.data["steps_run"] == 0


def test_cancelled_before_start(make_gui):
    adapter, backend = make_gui()
    token = CancelToken()
    token.cancel()
    assert run(adapter, "hotkey", token, keys=["a"]).status is Status.CANCELLED
    assert backend.calls == []


# ------------------------------------------------------- dispatcher wiring


def test_runs_on_gui_worker_and_release_jumps_queue(make_dispatcher, collector, make_gui):
    adapter, backend = make_gui(macros_yaml="name: slow\nsteps: [{wait: 0.2}, {hotkey: [b]}]")
    d = make_dispatcher()
    d.register_backend(Tier.GUI, adapter)
    d.start()
    gui = lambda op, **kw: make_request(op, type=ActionType.GUI_MACRO, **kw)  # noqa: E731
    hold = d.submit(gui("key_hold", args={"key": "alt"}))
    assert collector.wait_for(hold).ok  # otherwise release_all could legitimately jump ahead of the hold too
    ids = [hold, d.submit(gui("slow"))]
    ids.append(d.submit(gui("hotkey", args={"keys": ["a"]})))
    ids.append(d.submit(gui("release_all", priority=Priority.RELEASE)))
    results = collector.wait_all(ids, timeout=5)
    assert all(r.ok for r in results), [r.message for r in results]
    assert all(r.tier_used is Tier.GUI for r in results)
    # release_all ran before the queued hotkey 'a' (it jumped the normal queue)
    assert backend.kinds().index("key_up") < backend.calls.index(("hotkey", ("a",)))
    assert backend.threads == {d.worker(Tier.GUI).thread_ident}


def test_dispatcher_shutdown_releases_held_keys(make_dispatcher, collector, make_gui):
    adapter, backend = make_gui()
    d = make_dispatcher()
    d.register_backend(Tier.GUI, adapter)
    d.start()
    assert collector.wait_for(d.submit(make_request("key_hold", type=ActionType.GUI_MACRO,
                                                    args={"key": "shift"}))).ok
    assert d.shutdown(timeout=2)
    assert backend.calls[-1] == ("key_up", "shift")


# ------------------------------------------------------------- real backend


@pytest.mark.skipif(sys.platform != "win32", reason="PyAutoGUI checks run on the Windows dev box")
def test_real_backend_configures_pyautogui_without_injecting():
    from agc.engine.automation.adapters.gui_pyautogui import PyAutoGuiBackend

    backend = PyAutoGuiBackend()
    assert backend.check() == (True, "")
    import pyautogui

    assert pyautogui.PAUSE == 0 and pyautogui.FAILSAFE is False
    assert backend.is_valid_key("ctrl") and not backend.is_valid_key("nuke")
    x, y = backend.position()
    assert isinstance(x, int) and isinstance(y, int)


def test_gui_module_import_does_not_load_pyautogui_or_cv2():
    code = ("import sys; import agc.engine.automation.adapters.gui_pyautogui, agc.engine.automation.macros; "
            "bad = [m for m in ('pyautogui', 'cv2', 'win32clipboard') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=repo_root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
