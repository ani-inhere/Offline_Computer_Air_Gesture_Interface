"""Fakes for Tier 3 tests: recording input backend, scripted foreground probe, synthetic screens."""

from __future__ import annotations

import threading
from typing import Sequence

import numpy as np

from agc.engine.automation.interfaces import ForegroundInfo

VALID_KEYS = {"ctrl", "shift", "alt", "tab", "enter", "f12", "a", "b", "c", "v", "s", "n", "space", "esc"}


class RecordingBackend:
    """InputBackend that records every call instead of injecting input."""

    def __init__(self, *, paste_works: bool = True, fail_on: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.cursor = (100, 100)
        self.paste_works = paste_works
        self.fail_on = fail_on
        self.threads: set[int] = set()

    def _rec(self, *call) -> None:
        self.threads.add(threading.get_ident())
        if self.fail_on == call[0]:
            raise OSError(f"{call[0]} failed")
        self.calls.append(call)

    def check(self) -> tuple[bool, str]:
        return True, ""

    def is_valid_key(self, key: str) -> bool:
        return key in VALID_KEYS

    def key_down(self, key: str) -> None:
        self._rec("key_down", key)

    def key_up(self, key: str) -> None:
        self._rec("key_up", key)

    def hotkey(self, keys: Sequence[str]) -> None:
        self._rec("hotkey", tuple(keys))

    def press(self, key: str, presses: int) -> None:
        self._rec("press", key, presses)

    def write(self, text: str) -> None:
        self._rec("write", text)

    def paste(self, text: str) -> bool:
        if not self.paste_works:
            return False
        self._rec("paste", text)
        return True

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        self._rec("click", x, y, button, clicks)
        self.cursor = (x, y)

    def mouse_up(self, button: str) -> None:
        self._rec("mouse_up", button)

    def move(self, x: int, y: int) -> None:
        self._rec("move", x, y)
        self.cursor = (x, y)

    def position(self) -> tuple[int, int]:
        return self.cursor

    def scroll(self, clicks: int, x: int | None, y: int | None) -> None:
        self._rec("scroll", clicks, x, y)

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


class ScriptedProbe:
    """ForegroundProbe returning queued infos in turn; the last one repeats."""

    def __init__(self, *infos: ForegroundInfo | None) -> None:
        self.infos = list(infos) or [None]
        self.calls = 0

    def current(self) -> ForegroundInfo | None:
        info = self.infos[min(self.calls, len(self.infos) - 1)]
        self.calls += 1
        return info


def window(process: str = "WINWORD.EXE", title: str = "Doc1 - Word", elevated: bool = False,
           rect: tuple[int, int, int, int] | None = (100, 50, 800, 600)) -> ForegroundInfo:
    return ForegroundInfo(process=process, title=title, pid=1234, elevated=elevated, rect=rect)


def noise_screen(height: int = 400, width: int = 600, seed: int = 0) -> np.ndarray:
    """Smooth-ish random grayscale background (blurred noise, like UI chrome)."""
    import cv2

    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (height, width), dtype=np.uint8)
    return cv2.GaussianBlur(base, (7, 7), 0)


def distinctive_template(height: int = 30, width: int = 48, seed: int = 1) -> np.ndarray:
    """High-contrast blocky pattern, like a button icon."""
    import cv2

    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 2, (height // 6, width // 6), dtype=np.uint8) * 255
    return cv2.resize(blocks, (width, height), interpolation=cv2.INTER_NEAREST)


def paste(screen: np.ndarray, patch: np.ndarray, left: int, top: int) -> np.ndarray:
    out = screen.copy()
    out[top:top + patch.shape[0], left:left + patch.shape[1]] = patch
    return out
