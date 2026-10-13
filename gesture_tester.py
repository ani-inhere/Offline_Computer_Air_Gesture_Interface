"""Gesture tester: fire sample gestures by name and watch the automation backend act on them.

There is no live gesture recognition wired in yet (that is Phase 5), so this
script stands in for the camera: you pick a gesture, it builds the
ActionRequest a profile would produce, and submits it to the real
ActionDispatcher with the real Word (COM) and GUI (PyAutoGUI) adapters.

    python gesture_tester.py                 # interactive menu, real actions
    python gesture_tester.py --dry-run       # interactive, nothing really happens
    python gesture_tester.py --list          # show the gesture -> action table
    python gesture_tester.py --run TAP,DOUBLE_TAP,LONG_PINCH --delay 2

See docs/project_guide.md for a tutorial.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agc.engine.automation import (
    ActionDispatcher,
    ActionRequest,
    ActionResult,
    ActionType,
    CancelToken,
    Priority,
    Status,
    Tier,
)
from agc.engine.automation.adapters.base import AppAdapter, GuiAdapter
from agc.engine.automation.adapters.gui_pyautogui import GuiAutomationAdapter
from agc.engine.automation.adapters.word_com import WordAdapter, WordConfig


@dataclass(frozen=True)
class Binding:
    """One sample gesture binding (a stand-in for the Phase 4 profile file)."""

    gesture: str
    label: str
    type: ActionType
    op: str
    app: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    needs_focus: str | None = None   # window to click before the action fires (GUI input goes to the focused app)
    timeout_s: float = 10.0
    dedupe_key: str | None = None


WORD = ActionType.APP_API
GUI = ActionType.GUI_MACRO

BINDINGS = [
    # Word over COM: works without focusing Word.
    Binding("DOUBLE_TAP", "Word: new blank document", WORD, "new_document", "word"),
    Binding("TAP", "Word: insert the 'meeting_header' snippet", WORD, "insert_text", "word",
            {"snippet_id": "meeting_header"}),
    Binding("PINCH_TEXT", "Word: insert a line of sample text", WORD, "insert_text", "word",
            {"text": "Typed by Air Gesture Control.\n"}),
    Binding("TILT_U", "Word: make the last paragraph Heading 1", WORD, "set_style", "word", {"style": "Heading 1"}),
    Binding("LONG_PINCH", "Word: export PDF to ~/Documents/AGC", WORD, "export_pdf", "word",
            {"name_template": "agc_{timestamp}"}, timeout_s=60),
    Binding("TILT_R", "Word: save as .docx to ~/Documents/AGC", WORD, "save_as_docx", "word",
            {"name_template": "agc_{timestamp}"}, timeout_s=60),
    Binding("TILT_L", "Word: status (installed / running / document)", WORD, "status", "word"),
    # GUI tier (PyAutoGUI). Media keys reach whichever player owns media focus.
    Binding("FIST_HOLD", "Media: play / pause", GUI, "hotkey", args={"keys": ["playpause"]}),
    Binding("SWIPE_R", "Media: next track", GUI, "hotkey", args={"keys": ["nexttrack"]}),
    Binding("SWIPE_L", "Media: previous track", GUI, "hotkey", args={"keys": ["prevtrack"]}),
    Binding("FLICK_R", "Browser: next tab (macro)", GUI, "browser_next_tab", needs_focus="your browser"),
    Binding("FLICK_L", "Browser: previous tab (macro)", GUI, "browser_prev_tab", needs_focus="your browser"),
    Binding("SCROLL_2F", "Scroll down 3 clicks", GUI, "scroll", args={"clicks": -3},
            needs_focus="the window to scroll", dedupe_key="scroll"),
]
BY_GESTURE = {b.gesture: b for b in BINDINGS}


# --------------------------------------------------------------- dry run


class _DryRun:
    """Prints what would happen instead of touching Word or the keyboard."""

    def is_available(self) -> tuple[bool, str]:
        return True, ""

    def execute(self, request: ActionRequest, cancel_token: CancelToken) -> ActionResult:
        time.sleep(0.2)  # pretend to work so queueing is visible
        detail = f"{request.op} {request.args}" if request.args else request.op
        return ActionResult.for_request(request, Status.OK, f"[dry run] would do: {detail}")


class DryWord(_DryRun, AppAdapter):
    app = "word"
    name = "dry-word"


class DryGui(_DryRun, GuiAdapter):
    name = "dry-gui"


# ------------------------------------------------------------------ main


class Tester:
    def __init__(self, dry_run: bool, word_visible: bool, countdown: float) -> None:
        self.dry_run = dry_run
        self.countdown = countdown
        self.pending: dict[str, str] = {}
        self.lock = threading.Lock()
        self.done = threading.Condition(self.lock)
        self.dispatcher = ActionDispatcher()
        self.dispatcher.set_result_callback(self.on_result)
        if dry_run:
            self.dispatcher.register_backend(Tier.APP_API, DryWord())
            self.dispatcher.register_backend(Tier.GUI, DryGui())
        else:
            self.dispatcher.register_backend(Tier.APP_API, WordAdapter(WordConfig(visible=word_visible)))
            self.dispatcher.register_backend(Tier.GUI, GuiAutomationAdapter())
        self.dispatcher.start()

    def on_result(self, result: ActionResult) -> None:
        """Runs on the dispatcher's notifier thread (like a HUD toast would)."""
        with self.lock:
            gesture = self.pending.pop(result.id, "?")
            mark = "OK " if result.ok else result.status.value.upper()
            print(f"\n  <- {gesture}: [{mark}] {result.message}  ({result.duration_ms:.0f} ms)")
            if result.data:
                shown = {k: v for k, v in result.data.items() if k not in ("macro",)}
                if shown:
                    print(f"     data: {shown}")
            self.done.notify_all()

    def fire(self, gesture: str) -> str | None:
        binding = BY_GESTURE.get(gesture.upper())
        if binding is None:
            print(f"  unknown gesture {gesture!r} (type 'l' to list)")
            return None
        if binding.needs_focus and not self.dry_run and self.countdown > 0:
            print(f"  Click {binding.needs_focus} now - firing in", end="", flush=True)
            for n in range(int(self.countdown), 0, -1):
                print(f" {n}", end="", flush=True)
                time.sleep(1)
            print()
        request = ActionRequest(type=binding.type, app=binding.app, op=binding.op, args=binding.args,
                                timeout_s=binding.timeout_s, dedupe_key=binding.dedupe_key)
        with self.lock:
            self.pending[request.id] = binding.gesture
        t0 = time.perf_counter()
        job_id = self.dispatcher.submit(request)  # what the camera loop will call: never blocks
        took_us = (time.perf_counter() - t0) * 1e6
        print(f"  -> {binding.gesture}: {binding.label}  (submit returned in {took_us:.0f} us)")
        return job_id

    def release_all(self) -> None:
        request = ActionRequest(type=GUI, op="release_all", priority=Priority.RELEASE)
        with self.lock:
            self.pending[request.id] = "RELEASE_ALL"
        self.dispatcher.submit(request)

    def wait_idle(self, timeout: float = 90) -> None:
        end = time.monotonic() + timeout
        with self.lock:
            while self.pending and time.monotonic() < end:
                self.done.wait(0.2)

    def stats(self) -> None:
        for tier, info in self.dispatcher.stats()["workers"].items():
            print(f"  {tier:8} health={info['health']:10} queued={info['queue_normal']} "
                  f"release_queued={info['queue_release']} restarts={info['restarts']} "
                  f"running={info['current_op']}")

    def close(self) -> None:
        print("Shutting down (releasing keys, closing workers)...")
        ok = self.dispatcher.shutdown(timeout=10)
        print("Done." if ok else "Some workers did not stop in time.")


def print_table() -> None:
    print(f"\n  {'#':>2}  {'GESTURE':<11} {'TIER':<8} ACTION")
    for i, b in enumerate(BINDINGS, 1):
        tier = "word" if b.type is WORD else "gui"
        focus = f"   (click {b.needs_focus} first)" if b.needs_focus else ""
        print(f"  {i:>2}  {b.gesture:<11} {tier:<8} {b.label}{focus}")
    print()


def interactive(tester: Tester) -> None:
    print_table()
    print("Type a gesture name or number. Commands: l=list  s=stats  r=release all  q=quit")
    while True:
        try:
            choice = input("\ngesture> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            continue
        lower = choice.lower()
        if lower in ("q", "quit", "exit"):
            return
        if lower in ("l", "list"):
            print_table()
        elif lower in ("s", "stats"):
            tester.stats()
        elif lower in ("r", "release"):
            tester.release_all()
        elif choice.isdigit() and 1 <= int(choice) <= len(BINDINGS):
            tester.fire(BINDINGS[int(choice) - 1].gesture)
        else:
            tester.fire(choice)
        time.sleep(0.05)  # let quick results print before the next prompt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print actions instead of performing them")
    parser.add_argument("--list", action="store_true", help="show the gesture table and exit")
    parser.add_argument("--run", metavar="G1,G2,...", help="fire these gestures in order, then exit")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between gestures with --run")
    parser.add_argument("--countdown", type=float, default=3, help="seconds to focus a window for GUI gestures")
    parser.add_argument("--hidden-word", action="store_true", help="run Word in the background (default: visible)")
    args = parser.parse_args(argv)

    if args.list:
        print_table()
        return 0
    if args.run:
        unknown = [g for g in args.run.split(",") if g.strip().upper() not in BY_GESTURE]
        if unknown:
            parser.error(f"unknown gesture(s): {', '.join(unknown)}")

    mode = "DRY RUN (nothing will really happen)" if args.dry_run else "LIVE (Word and keyboard/mouse are real)"
    print(f"Air Gesture Control - gesture tester [{mode}]")
    tester = Tester(args.dry_run, word_visible=not args.hidden_word, countdown=args.countdown)
    try:
        if args.run:
            for gesture in (g.strip() for g in args.run.split(",")):
                tester.fire(gesture)
                time.sleep(args.delay)
            tester.wait_idle()
        else:
            interactive(tester)
            tester.wait_idle(timeout=15)
    finally:
        tester.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
