"""Macro schema and runner: step order, foreground checks, cancel, cursor arbitration."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fake_gui import ScriptedProbe, window

from agc.engine.automation import CancelToken, Status
from agc.engine.automation.cursor import CursorArbiter
from agc.engine.automation.locator import LocatorError, Match
from agc.engine.automation.macros import MacroError, MacroLibrary, MacroRunner


class FakeActions:
    """GuiActions recorder (stands in for the GUI adapter)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.cursor = (5, 5)

    def hotkey(self, keys):
        self.calls.append(("hotkey", tuple(keys)))

    def type_text(self, text):
        self.calls.append(("type", text))

    def click_at(self, x, y, button="left", clicks=1):
        self.calls.append(("click", x, y, button, clicks))
        self.cursor = (x, y)

    def scroll(self, clicks, x=None, y=None):
        self.calls.append(("scroll", clicks))

    def move(self, x, y):
        self.calls.append(("move", x, y))
        self.cursor = (x, y)

    def position(self):
        return self.cursor


class FakeLocator:
    def __init__(self, found: dict[str, Match | None] | None = None, delay_s: float = 0.0) -> None:
        self.found = found or {}
        self.delay_s = delay_s
        self.queries: list[tuple[str, float]] = []

    def find_on_screen(self, template, region=None, confidence=0.85, timeout_s=0.0, cancel_token=None, **_):
        self.queries.append((template, timeout_s))
        if self.delay_s and cancel_token is not None:
            cancel_token.wait(self.delay_s)
            cancel_token.raise_if_cancelled()
        if template not in self.found:
            raise LocatorError(f"template {template!r} not found in templates")
        return self.found[template]


def hit(x: int, y: int) -> Match:
    return Match(x=x, y=y, score=0.97, scale=1.0, width=20, height=10)


def load(yaml_text: str):
    library = MacroLibrary()
    (name,) = library.add_yaml(yaml_text)
    return library.get(name)


def runner(probe=None, locator=None, cursor=None, tmp_path: Path | None = None, actions=None):
    actions = actions or FakeActions()
    return actions, MacroRunner(actions, locator or FakeLocator(), probe or ScriptedProbe(window()),
                                cursor or CursorArbiter(), output_dir=tmp_path or Path("."))


EXPORT = """
name: export
requires_foreground: { process: ["winword.exe"] }
steps:
  - hotkey: [f12]
  - wait_for_image: dialog.png
    timeout: 3
  - click_image: type_box.png
    offset: [5, 0]
  - type: "PDF"
  - click_at_relative: { x: 0.5, y: 0.1, units: fraction }
  - scroll: -2
"""


# ----------------------------------------------------------------- schema


def test_shipped_macros_load():
    library = MacroLibrary.from_dir()
    assert library.errors == {}
    assert {"browser_next_tab", "browser_prev_tab", "word_export_pdf_macro"} <= set(library.macros)


@pytest.mark.parametrize("text, message", [
    ("name: m\nsteps: [{teleport: 1}]", "step 1"),
    ("name: m\nsteps: [{hotkey: [a], wait: 1}]", "exactly one action key"),
    ("name: m\nsteps: []", "at least 1"),
    ("name: 'bad name!'\nsteps: [{hotkey: [a]}]", "name"),
    ("name: m\nsteps: [{click_image: ../../etc/x.png}]", "plain image file name"),
    ("name: m\nsteps: [{wait: 60}]", "less than or equal to 10"),
    ("name: m\nsteps: [{type: {}}]", "either text or output_path"),
    ("name: m\nrequires_foreground: {title_regex: '('}\nsteps: [{hotkey: [a]}]", "requires_foreground"),
    ("name: m\nsteps: [{hotkey: [a]}]\nshell: rm -rf /", "Extra inputs"),
    (": : not yaml", "invalid YAML"),
])
def test_invalid_macros_are_rejected_with_location(text, message):
    with pytest.raises(MacroError, match=message):
        MacroLibrary().add_yaml(text)


def test_bad_file_does_not_hide_good_ones(tmp_path):
    (tmp_path / "good.yaml").write_text("name: good\nsteps: [{hotkey: [a]}]")
    (tmp_path / "bad.yaml").write_text("name: bad\nsteps: [{explode: true}]")
    library = MacroLibrary.from_dir(tmp_path)
    assert "good" in library and "bad" not in library
    assert "bad.yaml" in library.errors


def test_duplicate_names_rejected():
    library = MacroLibrary()
    library.add_yaml("name: m\nsteps: [{hotkey: [a]}]")
    with pytest.raises(MacroError, match="duplicate"):
        library.add_yaml("name: m\nsteps: [{hotkey: [b]}]")


# ----------------------------------------------------------------- runner


def test_runs_steps_in_order_and_restores_cursor():
    locator = FakeLocator({"dialog.png": hit(1, 1), "type_box.png": hit(400, 300)})
    actions, run = runner(locator=locator)
    outcome = run.run(load(EXPORT), CancelToken())
    assert outcome.status is Status.OK and outcome.steps_run == 6
    assert actions.calls == [
        ("hotkey", ("f12",)),
        ("click", 405, 300, "left", 1),
        ("move", 5, 5),                       # back to where the user's cursor was
        ("type", "PDF"),
        ("click", 100 + 400, 50 + 60, "left", 1),  # 50% x 10% of the 800x600 window at (100, 50)
        ("move", 5, 5),
        ("scroll", -2),
    ]
    assert locator.queries[0] == ("dialog.png", 3.0)


def test_aborts_before_start_when_foreground_does_not_match():
    actions, run = runner(probe=ScriptedProbe(window("notepad.exe")))
    outcome = run.run(load(EXPORT), CancelToken())
    assert outcome.status is Status.FAILED and outcome.failed_step is None
    assert "foreground is notepad.exe" in outcome.message and actions.calls == []


def test_aborts_mid_macro_when_foreground_changes():
    # probe calls: before start, step 1, step 2 -> user switched apps before step 2
    probe = ScriptedProbe(window(), window(), window("chrome.exe"))
    locator = FakeLocator({"dialog.png": hit(1, 1), "type_box.png": hit(2, 2)})
    actions, run = runner(probe=probe, locator=locator)
    outcome = run.run(load(EXPORT), CancelToken())
    assert outcome.status is Status.FAILED and outcome.failed_step == 2 and outcome.steps_run == 1
    assert "step 2 (wait_for_image dialog.png)" in outcome.message
    assert "changed to chrome.exe" in outcome.message
    assert actions.calls == [("hotkey", ("f12",))]


def test_missing_image_reports_failed_step():
    locator = FakeLocator({"dialog.png": None})
    _, run = runner(locator=locator)
    outcome = run.run(load(EXPORT), CancelToken())
    assert outcome.failed_step == 2 and "did not appear within 3s" in outcome.message


def test_missing_template_file_reports_capture_hint():
    _, run = runner(locator=FakeLocator({}))
    outcome = run.run(load(EXPORT), CancelToken())
    assert outcome.status is Status.FAILED and outcome.failed_step == 2 and "not found" in outcome.message


def test_cancel_mid_macro_stops_between_steps():
    macro = load("name: m\nsteps: [{hotkey: [a]}, {wait: 5}, {hotkey: [b]}]")
    actions, run = runner(probe=ScriptedProbe(None))
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    t0 = time.monotonic()
    outcome = run.run(macro, token)
    assert time.monotonic() - t0 < 1.0
    assert outcome.status is Status.CANCELLED and outcome.failed_step == 2
    assert actions.calls == [("hotkey", ("a",))]


def test_timeout_while_waiting_for_image():
    locator = FakeLocator({"dialog.png": hit(1, 1)}, delay_s=5)
    _, run = runner(locator=locator)
    token = CancelToken()
    threading.Timer(0.05, token.cancel, args=("timeout",)).start()
    outcome = run.run(load(EXPORT), token)
    assert outcome.status is Status.TIMEOUT and outcome.failed_step == 2


def test_refuses_when_pointer_mode_holds_cursor():
    cursor = CursorArbiter()
    cursor.set_pointer_active(True)
    macro = load(EXPORT).model_copy(update={"cursor_wait_s": 0.05})
    actions, run = runner(cursor=cursor, locator=FakeLocator({"dialog.png": hit(1, 1)}))
    outcome = run.run(macro, CancelToken())
    assert outcome.status is Status.FAILED and "Pointer mode is using the cursor" in outcome.message
    assert actions.calls == []


def test_waits_briefly_for_pointer_mode_to_end():
    cursor = CursorArbiter()
    cursor.set_pointer_active(True)
    threading.Timer(0.05, cursor.set_pointer_active, args=(False,)).start()
    locator = FakeLocator({"dialog.png": hit(1, 1), "type_box.png": hit(2, 2)})
    _, run = runner(cursor=cursor, locator=locator)
    assert run.run(load(EXPORT), CancelToken()).status is Status.OK
    assert cursor.owner is None  # released afterwards


def test_pointer_mode_taking_over_mid_macro_aborts_cursor_steps():
    cursor = CursorArbiter()

    class GrabbingActions(FakeActions):
        def hotkey(self, keys):
            super().hotkey(keys)
            cursor.set_pointer_active(True)  # user enters Pointer mode during step 1

    locator = FakeLocator({"dialog.png": hit(1, 1), "type_box.png": hit(2, 2)})
    actions, run = runner(cursor=cursor, locator=locator, actions=GrabbingActions())
    outcome = run.run(load(EXPORT), CancelToken())
    assert outcome.status is Status.FAILED and outcome.failed_step == 3
    assert "Pointer mode took over" in outcome.message
    assert not any(call[0] == "click" for call in actions.calls)


def test_elevated_foreground_is_refused():
    _, run = runner(probe=ScriptedProbe(window(elevated=True)))
    outcome = run.run(load(EXPORT), CancelToken())
    assert outcome.status is Status.FAILED and "runs as administrator" in outcome.message


def test_assert_foreground_step():
    macro = load("name: m\nsteps: [{hotkey: [a]}, {assert_foreground: {title_regex: 'Report'}}, {hotkey: [b]}]")
    actions, run = runner(probe=ScriptedProbe(window(title="Notes - Word")))
    outcome = run.run(macro, CancelToken())
    assert outcome.failed_step == 2 and "expected title" in outcome.message
    assert actions.calls == [("hotkey", ("a",))]


def test_click_at_relative_needs_window_rect():
    macro = load("name: m\nsteps: [{click_at_relative: [10, 20]}]")
    _, run = runner(probe=ScriptedProbe(window(rect=None)))
    outcome = run.run(macro, CancelToken())
    assert outcome.failed_step == 1 and "position is unknown" in outcome.message


def test_type_output_path_stays_in_allowed_folder(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "export.pdf").write_text("existing")
    macro = load("name: m\nsteps: [{type: {output_path: {suffix: .pdf, name_template: export}}}]")
    actions, run = runner(tmp_path=tmp_path / "out")
    assert run.run(macro, CancelToken()).status is Status.OK
    typed = Path(actions.calls[0][1])
    assert typed == (tmp_path / "out").resolve() / "export-1.pdf"
    assert not typed.exists()  # not pre-created: the app's Save dialog writes it


def test_type_output_path_rejects_escape(tmp_path):
    macro = load("name: m\nsteps: [{type: {output_path: {suffix: .pdf, out_dir: '../..'}}}]")
    _, run = runner(tmp_path=tmp_path / "out")
    outcome = run.run(macro, CancelToken())
    assert outcome.status is Status.FAILED and "must be inside" in outcome.message
