"""Tier 3 adapter: keyboard/mouse injection with PyAutoGUI, run only on the GUI worker thread.

Ops (``request.args`` in brackets):

* ``hotkey`` [keys], ``press`` [key, presses]
* ``key_hold`` [key] / ``key_release`` [key]  - holds are tracked in an input ledger
* ``type_text`` [text | snippet_id]           - long or non-ASCII text is pasted via the clipboard
* ``click_at`` [x, y, button, clicks], ``move`` [x, y], ``scroll`` [clicks, x, y]
* ``release_all``                             - release every held key/button (send with priority=release)
* ``run_macro`` [macro], or the macro name itself as the op

PyAutoGUI is configured on first load with ``PAUSE = 0`` and ``FAILSAFE =
False`` (PRD ACT-1/ACT-2). :meth:`GuiAutomationAdapter.release_all` uses only
the ledger's own lock, never ``gui_lock``, so it is safe from any thread even
while a macro is running. Input aimed at an elevated window is refused with a
clear message instead of failing silently (PRD ACT-7).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..cancel import CancelledError, CancelToken
from ..cursor import CursorArbiter, CursorBusyError
from ..interfaces import ForegroundProbe
from ..locator import DEFAULT_TEMPLATE_DIR, ScreenLocator
from ..macros import DEFAULT_MACROS_DIR, MacroError, MacroLibrary, MacroRunner, elevated_message
from ..models import ActionRequest, ActionResult, Status
from ..outputs import DEFAULT_OUTPUT_DIR
from ..snippets import DEFAULT_SNIPPETS_PATH, SnippetError, SnippetStore
from ..win32_util import current_process_elevated, default_foreground_probe, ensure_dpi_aware
from .base import GuiAdapter

log = logging.getLogger("agc.automation.gui")

CURSOR_OWNER = "gui-adapter"
BUTTONS = ("left", "right", "middle")


class InputError(Exception):
    """Invalid input request (bad key, missing argument...); becomes a FAILED result."""


class InputBackend(Protocol):
    """Raw input injection. :class:`PyAutoGuiBackend` in production, a recorder in tests."""

    def check(self) -> tuple[bool, str]: ...
    def is_valid_key(self, key: str) -> bool: ...
    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def hotkey(self, keys: Sequence[str]) -> None: ...
    def press(self, key: str, presses: int) -> None: ...
    def write(self, text: str) -> None: ...
    def paste(self, text: str) -> bool: ...
    def click(self, x: int, y: int, button: str, clicks: int) -> None: ...
    def mouse_up(self, button: str) -> None: ...
    def move(self, x: int, y: int) -> None: ...
    def position(self) -> tuple[int, int]: ...
    def scroll(self, clicks: int, x: int | None, y: int | None) -> None: ...


class PyAutoGuiBackend:
    """:class:`InputBackend` over PyAutoGUI, imported and configured on first use."""

    def __init__(self) -> None:
        self._pg: Any = None
        self._load_lock = threading.Lock()

    def _lib(self) -> Any:
        if self._pg is None:
            with self._load_lock:
                if self._pg is None:
                    ensure_dpi_aware()  # before PyAutoGUI sets a weaker (system) DPI awareness
                    import pyautogui

                    pyautogui.PAUSE = 0          # ACT-1: default 0.1 s per call ruins latency
                    pyautogui.FAILSAFE = False   # ACT-2: corner abort would fire at air-mouse edges
                    pyautogui.MINIMUM_DURATION = 0
                    self._pg = pyautogui
        return self._pg

    def check(self) -> tuple[bool, str]:
        try:
            self._lib()
            return True, ""
        except Exception as exc:  # noqa: BLE001 - ImportError, or no display on Linux
            return False, f"PyAutoGUI unavailable: {type(exc).__name__}: {exc}"

    def is_valid_key(self, key: str) -> bool:
        return key in self._lib().KEYBOARD_KEYS

    def key_down(self, key: str) -> None:
        self._lib().keyDown(key)

    def key_up(self, key: str) -> None:
        self._lib().keyUp(key)

    def hotkey(self, keys: Sequence[str]) -> None:
        self._lib().hotkey(*keys)

    def press(self, key: str, presses: int) -> None:
        self._lib().press(key, presses=presses)

    def write(self, text: str) -> None:
        self._lib().write(text)

    def paste(self, text: str) -> bool:
        """Put ``text`` on the clipboard, Ctrl+V, then restore the previous clipboard text."""
        try:
            import win32clipboard  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
        except ImportError:
            return False
        previous = None
        if not _open_clipboard(win32clipboard):
            return False
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                previous = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        self.hotkey(["ctrl", "v"])
        time.sleep(0.15)  # the target app reads the clipboard asynchronously
        if previous is not None and _open_clipboard(win32clipboard):
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, previous)
            finally:
                win32clipboard.CloseClipboard()
        return True

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        self._lib().click(x=x, y=y, clicks=clicks, button=button)

    def mouse_up(self, button: str) -> None:
        self._lib().mouseUp(button=button)

    def move(self, x: int, y: int) -> None:
        self._lib().moveTo(x, y)

    def position(self) -> tuple[int, int]:
        point = self._lib().position()
        return int(point[0]), int(point[1])

    def scroll(self, clicks: int, x: int | None, y: int | None) -> None:
        self._lib().scroll(clicks, x=x, y=y)


def _open_clipboard(win32clipboard: Any) -> bool:
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:  # noqa: BLE001 - another app holds the clipboard
            time.sleep(0.02)
    return False


class InputLedger:
    """Keys and mouse buttons currently held down by AGC (PRD ACT-4). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: list[str] = []
        self._buttons: list[str] = []

    def hold_key(self, key: str) -> None:
        with self._lock:
            if key not in self._keys:
                self._keys.append(key)

    def drop_key(self, key: str) -> None:
        with self._lock:
            if key in self._keys:
                self._keys.remove(key)

    def hold_button(self, button: str) -> None:
        with self._lock:
            if button not in self._buttons:
                self._buttons.append(button)

    def drop_button(self, button: str) -> None:
        with self._lock:
            if button in self._buttons:
                self._buttons.remove(button)

    def take_all(self) -> tuple[list[str], list[str]]:
        """Remove and return ``(keys, buttons)``; keys in reverse press order."""
        with self._lock:
            keys, buttons = list(reversed(self._keys)), list(self._buttons)
            self._keys.clear()
            self._buttons.clear()
            return keys, buttons

    @property
    def held(self) -> tuple[list[str], list[str]]:
        with self._lock:
            return list(self._keys), list(self._buttons)


class GuiConfig(BaseModel):
    """Settings for :class:`GuiAutomationAdapter`."""

    model_config = ConfigDict(extra="forbid")

    paste_threshold: int = Field(20, ge=0, description="Paste instead of typing above this many characters")
    max_text_chars: int = Field(5000, gt=0)
    cursor_wait_s: float = Field(1.0, ge=0, le=10)
    restore_cursor: bool = True
    macros_dir: Path = DEFAULT_MACROS_DIR
    template_dir: Path = DEFAULT_TEMPLATE_DIR
    allowed_output_dir: Path = DEFAULT_OUTPUT_DIR
    snippets_path: Path = DEFAULT_SNIPPETS_PATH


class GuiAutomationAdapter(GuiAdapter):
    """PyAutoGUI-backed Tier 3 adapter; also the :class:`~agc.engine.automation.macros.GuiActions` for macros."""

    name = "gui-pyautogui"
    primitive_ops = frozenset({
        "hotkey", "press", "key_hold", "key_release", "type_text", "click_at", "move", "scroll",
        "release_all", "run_macro",
    })
    #: Ops that only release input: always allowed, even toward elevated windows.
    release_ops = frozenset({"release_all", "key_release"})

    def __init__(self, config: GuiConfig | None = None, *, backend: InputBackend | None = None,
                 probe: ForegroundProbe | None = None, cursor: CursorArbiter | None = None,
                 locator: ScreenLocator | None = None, macros: MacroLibrary | None = None,
                 self_elevated: bool | None = None) -> None:
        self.config = config or GuiConfig()
        self.backend: InputBackend = backend or PyAutoGuiBackend()
        self.probe: ForegroundProbe = probe or default_foreground_probe()
        self.cursor = cursor or CursorArbiter()
        self.locator = locator or ScreenLocator(self.config.template_dir)
        self._macros = macros
        self.self_elevated = current_process_elevated() if self_elevated is None else self_elevated
        self.ledger = InputLedger()
        self.snippets = SnippetStore(self.config.snippets_path)
        self._handlers: dict[str, Callable[[ActionRequest, CancelToken], ActionResult]] = {
            "hotkey": self._op_hotkey, "press": self._op_press, "key_hold": self._op_key_hold,
            "key_release": self._op_key_release, "type_text": self._op_type_text, "click_at": self._op_click_at,
            "move": self._op_move, "scroll": self._op_scroll, "release_all": self._op_release_all,
            "run_macro": self._op_run_macro,
        }

    @property
    def macros(self) -> MacroLibrary:
        if self._macros is None:
            self._macros = MacroLibrary.from_dir(self.config.macros_dir)
        return self._macros

    # ------------------------------------------------------------ adapter API

    def handles(self, request: ActionRequest) -> bool:
        return request.op in self.primitive_ops or request.op in self.macros

    def is_available(self) -> tuple[bool, str]:
        return self.backend.check()

    def execute(self, request: ActionRequest, cancel_token: CancelToken) -> ActionResult:
        """Run one op. Never raises."""
        op = request.op
        handler = self._handlers.get(op)
        if handler is None and op in self.macros:
            handler = self._op_run_macro
        if handler is None:
            return ActionResult.for_request(request, Status.FAILED, f"unknown GUI op {op!r}")
        try:
            cancel_token.raise_if_cancelled()
            if op not in self.release_ops:
                info = self.probe.current()
                if info is not None and info.elevated and not self.self_elevated:
                    return ActionResult.for_request(request, Status.FAILED, elevated_message(info),
                                                    {"blocked_by": "elevated_window", "process": info.process})
            return handler(request, cancel_token)
        except CancelledError as exc:
            status = Status.TIMEOUT if exc.reason == "timeout" else Status.CANCELLED
            return ActionResult.for_request(request, status, exc.reason)
        except (InputError, SnippetError, MacroError, CursorBusyError) as exc:
            return ActionResult.for_request(request, Status.FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - contract: never raise (ACT-3)
            log.warning("GUI op %s failed: %s", op, type(exc).__name__)
            return ActionResult.for_request(request, Status.FAILED, f"input failed: {type(exc).__name__}: {exc}")

    def release_all(self) -> None:
        """Release every key and button AGC holds. Safe from any thread; never takes ``gui_lock``."""
        keys, buttons = self.ledger.take_all()
        for key in keys:
            try:
                self.backend.key_up(key)
            except Exception:  # noqa: BLE001 - keep releasing the rest
                log.exception("key_up(%s) failed during release_all", key)
        for button in buttons:
            try:
                self.backend.mouse_up(button)
            except Exception:  # noqa: BLE001
                log.exception("mouse_up(%s) failed during release_all", button)

    def shutdown(self) -> None:
        self.release_all()

    # ------------------------------------------- primitives (GuiActions for macros)

    def hotkey(self, keys: Sequence[str]) -> None:
        self.backend.hotkey(self._keys(keys))

    def type_text(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise InputError("text must be a non-empty string")
        if len(text) > self.config.max_text_chars:
            raise InputError(f"text is longer than {self.config.max_text_chars} characters")
        typeable = text.isascii() and all(c.isprintable() or c in "\n\t" for c in text)
        if len(text) > self.config.paste_threshold or not typeable:
            if self.backend.paste(text):
                return
            if not typeable:
                raise InputError("text has characters that cannot be typed and the clipboard is unavailable")
        self.backend.write(text)

    def click_at(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        if button not in BUTTONS:
            raise InputError(f"button must be one of {', '.join(BUTTONS)}")
        self.backend.click(int(x), int(y), button, int(clicks))

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> None:
        self.backend.scroll(int(clicks), x, y)

    def move(self, x: int, y: int) -> None:
        self.backend.move(int(x), int(y))

    def position(self) -> tuple[int, int]:
        return self.backend.position()

    # ------------------------------------------------------------------- ops

    def _op_hotkey(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        keys = request.args.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 5:
            raise InputError("hotkey needs keys: a list of 1-5 key names")
        self.hotkey(keys)
        return ActionResult.for_request(request, Status.OK, "+".join(keys))

    def _op_press(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        (key,) = self._keys([request.args.get("key")])
        presses = request.args.get("presses", 1)
        if not isinstance(presses, int) or not 1 <= presses <= 20:
            raise InputError("presses must be 1-20")
        self.backend.press(key, presses)
        return ActionResult.for_request(request, Status.OK, key)

    def _op_key_hold(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        (key,) = self._keys([request.args.get("key")])
        self.ledger.hold_key(key)  # record first: a crash mid-call must still be released
        self.backend.key_down(key)
        return ActionResult.for_request(request, Status.OK, f"holding {key}")

    def _op_key_release(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        (key,) = self._keys([request.args.get("key")])
        self.backend.key_up(key)
        self.ledger.drop_key(key)
        return ActionResult.for_request(request, Status.OK, f"released {key}")

    def _op_release_all(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        keys, buttons = self.ledger.held
        self.release_all()
        return ActionResult.for_request(request, Status.OK, "released all inputs",
                                        {"keys": keys, "buttons": buttons})

    def _op_type_text(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        text, snippet = request.args.get("text"), request.args.get("snippet_id")
        if (text is None) == (snippet is None):
            raise InputError("type_text needs exactly one of: text, snippet_id")
        self.type_text(text if text is not None else self.snippets.get(snippet))
        return ActionResult.for_request(request, Status.OK, "text typed")

    def _op_click_at(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        x, y = self._xy(request.args)
        with self.cursor.hold(CURSOR_OWNER, self.config.cursor_wait_s):
            home = self.position() if self.config.restore_cursor else None
            self.click_at(x, y, request.args.get("button", "left"), request.args.get("clicks", 1))
            if home is not None:
                self.move(*home)
        return ActionResult.for_request(request, Status.OK, f"clicked {x},{y}")

    def _op_move(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        x, y = self._xy(request.args)
        with self.cursor.hold(CURSOR_OWNER, self.config.cursor_wait_s):
            self.move(x, y)
        return ActionResult.for_request(request, Status.OK, f"moved to {x},{y}")

    def _op_scroll(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        clicks = request.args.get("clicks")
        if not isinstance(clicks, int) or not -50 <= clicks <= 50 or clicks == 0:
            raise InputError("scroll needs clicks: a non-zero integer in -50..50")
        if "x" in request.args or "y" in request.args:
            x, y = self._xy(request.args)
            with self.cursor.hold(CURSOR_OWNER, self.config.cursor_wait_s):
                home = self.position() if self.config.restore_cursor else None
                self.scroll(clicks, x, y)
                if home is not None:
                    self.move(*home)
        else:
            self.scroll(clicks)
        return ActionResult.for_request(request, Status.OK, f"scrolled {clicks}")

    def _op_run_macro(self, request: ActionRequest, token: CancelToken) -> ActionResult:
        name = request.args.get("macro", request.op if request.op != "run_macro" else None)
        if not isinstance(name, str):
            raise InputError("run_macro needs args.macro")
        if name not in self.macros:  # errors are keyed by file, so list them to help find a broken one
            broken = [f"{f}: {e}" for f, e in self.macros.errors.items()]
            raise MacroError(f"unknown macro {name!r}" + (f" (rejected files: {'; '.join(broken)})" if broken else ""))
        runner = MacroRunner(self, self.locator, self.probe, self.cursor,
                             output_dir=self.config.allowed_output_dir, self_elevated=self.self_elevated)
        outcome = runner.run(self.macros.get(name), token)
        data = {"macro": name, "steps_run": outcome.steps_run, **outcome.data}
        if outcome.failed_step is not None:
            data["failed_step"] = outcome.failed_step
        return ActionResult.for_request(request, outcome.status, outcome.message, data)

    # --------------------------------------------------------------- helpers

    def _keys(self, keys: Sequence[Any]) -> list[str]:
        out = []
        for key in keys:
            if not isinstance(key, str) or not self.backend.is_valid_key(key.lower()):
                raise InputError(f"unknown key {key!r}")
            out.append(key.lower())
        return out

    @staticmethod
    def _xy(args: dict[str, Any]) -> tuple[int, int]:
        x, y = args.get("x"), args.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            raise InputError("x and y must be integer screen coordinates")
        return x, y
