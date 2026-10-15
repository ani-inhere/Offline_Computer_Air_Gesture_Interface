"""GestureActionBridge: profile loading, gesture -> request mapping, cooldown, context, results."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fakes import FakeAppAdapter

from agc.engine.automation import ActionType, Status
from agc.engine.automation.adapters.browser import BrowserAdapter, BrowserConfig
from agc.engine.automation.adapters.word_com import WordAdapter, WordConfig
from agc.engine.automation.interfaces import ForegroundInfo
from agc.engine.gesture_bridge import DEFAULT_PROFILE_PATH, GestureActionBridge, load_profile_file

PROFILE = """
profiles:
  - id: default
    bindings:
      - gesture: TAP
        action: { type: app_api, app: fake, op: insert_text, args: { snippet_id: meeting_header } }
      - gesture: DOUBLE_TAP
        cooldown_s: 2.0
        action: { type: app_api, app: fake, op: new_document }
  - id: in_word
    match: { process: ["WINWORD.EXE"] }
    bindings:
      - gesture: TAP
        action: { type: app_api, app: fake, op: insert_paragraph }
"""


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Probe:
    def __init__(self, process: str = "python.exe") -> None:
        self.process = process

    def current(self) -> ForegroundInfo:
        return ForegroundInfo(process=self.process, title="")


class StatusLines:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._cond = threading.Condition()

    def __call__(self, line: str) -> None:
        with self._cond:
            self.lines.append(line)
            self._cond.notify_all()

    def wait(self, count: int, timeout: float = 3.0) -> list[str]:
        with self._cond:
            assert self._cond.wait_for(lambda: len(self.lines) >= count, timeout), self.lines
            return list(self.lines)


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE, encoding="utf-8")
    return path


@pytest.fixture
def make_bridge(profile: Path):
    created: list[GestureActionBridge] = []

    def factory(**kwargs) -> GestureActionBridge:
        kwargs.setdefault("adapters", [FakeAppAdapter()])
        kwargs.setdefault("probe", Probe())
        bridge = GestureActionBridge(profile, **kwargs)
        created.append(bridge)
        return bridge

    yield factory
    for bridge in created:
        bridge.shutdown(timeout=1.0)


def test_gesture_submits_bound_action_and_reports_result(make_bridge):
    adapter, status = FakeAppAdapter(), StatusLines()
    bridge = make_bridge(adapters=[adapter], on_status=status)
    bridge.start()

    job_id = bridge.on_gesture("TAP")

    assert job_id is not None
    lines = status.wait(1)
    assert lines == [f"TAP -> fake.insert_text: {Status.OK.value.upper()} - fake-app ran insert_text"]
    assert bridge.status == lines[0]
    assert adapter.executed_ops == ["insert_text"]


def test_unbound_gesture_is_ignored(make_bridge):
    assert make_bridge().on_gesture("LONG_PINCH") is None


def test_cooldown_suppresses_repeats_until_it_expires(make_bridge):
    clock = Clock()
    bridge = make_bridge(clock=clock)
    assert bridge.on_gesture("DOUBLE_TAP") is not None
    clock.now += 1.9
    assert bridge.on_gesture("DOUBLE_TAP") is None
    clock.now += 0.2
    assert bridge.on_gesture("DOUBLE_TAP") is not None


def test_foreground_app_selects_matching_profile(make_bridge):
    adapter, status = FakeAppAdapter(), StatusLines()
    probe = Probe("winword.exe")  # matched case-insensitively
    bridge = make_bridge(adapters=[adapter], probe=probe, on_status=status)
    bridge.start()

    bridge.on_gesture("TAP")
    status.wait(1)
    probe.process = "chrome.exe"
    bridge.on_gesture("TAP")
    status.wait(2)

    assert adapter.executed_ops == ["insert_paragraph", "insert_text"]


def test_matching_profile_without_the_gesture_falls_back_to_default(make_bridge):
    adapter, status = FakeAppAdapter(), StatusLines()
    bridge = make_bridge(adapters=[adapter], probe=Probe("WINWORD.EXE"), on_status=status)
    bridge.start()

    bridge.on_gesture("DOUBLE_TAP")
    status.wait(1)

    assert adapter.executed_ops == ["new_document"]


def test_build_request_copies_action_fields(profile):
    binding = load_profile_file(profile).profiles[0].bindings[0]
    first, second = GestureActionBridge.build_request(binding), GestureActionBridge.build_request(binding)
    assert (first.type, first.app, first.op) == (ActionType.APP_API, "fake", "insert_text")
    assert first.args == {"snippet_id": "meeting_header"}
    assert first.id != second.id


def test_binding_for_unknown_app_is_rejected(profile):
    with pytest.raises(ValueError, match="no adapter for app 'fake'"):
        GestureActionBridge(profile, adapters=[], probe=Probe())


def test_unknown_word_op_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("profiles:\n  - id: p\n    bindings:\n      - gesture: TAP\n"
                    "        action: { type: app_api, app: word, op: delete_everything }\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no op 'delete_everything'"):
        GestureActionBridge(path, adapters=[WordAdapter()], probe=Probe())


def test_invalid_yaml_schema_names_the_file(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("profiles:\n  - id: p\n    bindings:\n      - gesture: TAP\n        action: { op: x }\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="broken.yaml"):
        load_profile_file(path)


def test_shipped_word_profile_is_valid():
    config = load_profile_file(DEFAULT_PROFILE_PATH)
    adapters = [WordAdapter(WordConfig(**config.word)), BrowserAdapter(BrowserConfig(**config.browser))]
    bridge = GestureActionBridge(adapters=adapters, probe=Probe())
    try:
        assert bridge.gestures == ["DOUBLE_TAP", "LONG_PINCH", "PEACE_HOLD", "TAP"]
        ops = {b.gesture: b.action.op for b in config.profiles[0].bindings}
        assert ops == {"DOUBLE_TAP": "new_document", "TAP": "insert_text", "LONG_PINCH": "export_pdf"}
        peace = config.profiles[1].binding_for("PEACE_HOLD")
        assert (peace.action.app, peace.action.op) == ("browser", "open_url")
        assert config.browser["browser"] == "edge"
        assert peace.action.args["url"] == "https://github.com/ani-inhere/Offline_Computer_Air_Gesture_Interface"
    finally:
        bridge.shutdown(timeout=1.0)


def test_peace_hold_opens_the_bound_url(tmp_path, make_bridge):
    launched: list[list[str]] = []
    browser = BrowserAdapter(BrowserConfig(), launcher=launched.append,
                             finder=lambda name: Path("C:/Edge/msedge.exe"))
    status = StatusLines()
    bridge = GestureActionBridge(DEFAULT_PROFILE_PATH, adapters=[WordAdapter(), browser], probe=Probe(),
                                 on_status=status)
    try:
        bridge.start()
        assert bridge.on_gesture("PEACE_HOLD") is not None
        line = status.wait(1)[0]
        assert line == "PEACE_HOLD -> browser.open_url: OK - Opened github.com in Microsoft Edge"
        assert launched == [[str(Path("C:/Edge/msedge.exe")),
                             "https://github.com/ani-inhere/Offline_Computer_Air_Gesture_Interface"]]
    finally:
        bridge.shutdown(timeout=1.0)


@pytest.mark.parametrize("url, message", [
    ("javascript:alert(1)", "only http:// and https://"),
    ("--remote-debugging-port=9222", "only http:// and https://"),
    ("https://", "url has no host name"),
])
def test_bad_url_in_profile_is_rejected_at_load(tmp_path, url, message):
    path = tmp_path / "p.yaml"
    path.write_text("profiles:\n  - id: p\n    bindings:\n      - gesture: PEACE_HOLD\n"
                    f"        action: {{ type: app_api, app: browser, op: open_url, args: {{ url: '{url}' }} }}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match=f"gesture 'PEACE_HOLD': {message}"):
        GestureActionBridge(path, adapters=[BrowserAdapter()], probe=Probe())

