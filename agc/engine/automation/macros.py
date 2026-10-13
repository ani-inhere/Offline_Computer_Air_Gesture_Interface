"""YAML-defined GUI macros and the runner that plays them on the GUI worker thread.

Example (``agc/config/macros/*.yaml``)::

    name: word_export_pdf_macro
    requires_foreground: { process: ["WINWORD.EXE"] }
    steps:
      - hotkey: [f12]
      - wait_for_image: word_save_as_dialog.png
        timeout: 3
      - click_image: word_save_as_type.png

Step types: ``hotkey``, ``type``, ``wait``, ``wait_for_image``, ``click_image``,
``click_at_relative``, ``scroll``, ``assert_foreground``. Before every step the
runner checks the cancel token, that the foreground window still matches
``requires_foreground``, that it is not an elevated window (PRD ACT-7), and,
for cursor steps, that Pointer mode has not taken the cursor back.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Sequence, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .cancel import CancelledError, CancelToken
from .cursor import CursorArbiter
from .interfaces import ForegroundInfo, ForegroundProbe
from .locator import LocatorError, ScreenLocator
from .models import Status
from .outputs import DEFAULT_OUTPUT_DIR, OutputPathError, next_free_path, render_name, resolve_output_dir

log = logging.getLogger("agc.automation.macros")

DEFAULT_MACROS_DIR = Path(__file__).resolve().parents[2] / "config" / "macros"
CURSOR_OWNER = "gui-macro"
_NAME = r"^[A-Za-z0-9_-]{1,64}$"
_TEMPLATE = re.compile(r"^[\w\-. ]{1,100}\.(png|jpg|jpeg|bmp)$", re.IGNORECASE)


class MacroError(ValueError):
    """A macro file or definition is invalid, or a macro name is unknown."""


# ------------------------------------------------------------------ schema


class ForegroundSpec(BaseModel):
    """Which foreground window a macro may act on. Empty lists/None match anything."""

    model_config = ConfigDict(extra="forbid")

    process: list[str] = Field(default_factory=list)
    title_regex: str | None = None

    @field_validator("title_regex")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid title_regex: {exc}") from None
        return value

    def matches(self, info: ForegroundInfo | None) -> bool:
        if info is None:
            return False
        if self.process and info.process.lower() not in {p.lower() for p in self.process}:
            return False
        if self.title_regex and not re.search(self.title_regex, info.title):
            return False
        return True

    def describe(self) -> str:
        parts = []
        if self.process:
            parts.append("/".join(self.process))
        if self.title_regex:
            parts.append(f"title ~ {self.title_regex!r}")
        return " ".join(parts) or "any window"


Button = Literal["left", "right", "middle"]
Region = tuple[int, int, int, int]


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TemplateStep(_Step):
    template: str
    confidence: float = Field(0.85, ge=0.5, le=1.0)
    region: Region | None = None

    @field_validator("template")
    @classmethod
    def _safe_template(cls, value: str) -> str:
        if not _TEMPLATE.match(value):
            raise ValueError("template must be a plain image file name like 'save_dialog.png'")
        return value


class HotkeyStep(_Step):
    kind: Literal["hotkey"] = "hotkey"
    keys: list[str] = Field(min_length=1, max_length=5)


class OutputPathSpec(_Step):
    suffix: str = Field(pattern=r"^\.[A-Za-z0-9]{1,8}$")
    name_template: str = "agc_{timestamp}"
    out_dir: str | None = None


class TypeStep(_Step):
    kind: Literal["type"] = "type"
    text: str | None = Field(None, max_length=5000)
    output_path: OutputPathSpec | None = None

    @model_validator(mode="after")
    def _one_source(self) -> TypeStep:
        if (self.text is None) == (self.output_path is None):
            raise ValueError("type needs either text or output_path")
        return self


class WaitStep(_Step):
    kind: Literal["wait"] = "wait"
    seconds: float = Field(gt=0, le=10)


class WaitForImageStep(_TemplateStep):
    kind: Literal["wait_for_image"] = "wait_for_image"
    timeout: float = Field(3.0, ge=0, le=30)


class ClickImageStep(_TemplateStep):
    kind: Literal["click_image"] = "click_image"
    timeout: float = Field(2.0, ge=0, le=30)
    button: Button = "left"
    clicks: int = Field(1, ge=1, le=3)
    offset: tuple[int, int] = (0, 0)


class ClickAtRelativeStep(_Step):
    kind: Literal["click_at_relative"] = "click_at_relative"
    x: float
    y: float
    units: Literal["px", "fraction"] = "px"
    button: Button = "left"
    clicks: int = Field(1, ge=1, le=3)


class ScrollStep(_Step):
    kind: Literal["scroll"] = "scroll"
    clicks: int = Field(ge=-50, le=50)


class AssertForegroundStep(_Step):
    kind: Literal["assert_foreground"] = "assert_foreground"
    foreground: ForegroundSpec


Step = Annotated[
    Union[HotkeyStep, TypeStep, WaitStep, WaitForImageStep, ClickImageStep, ClickAtRelativeStep, ScrollStep,
          AssertForegroundStep],
    Field(discriminator="kind"),
]
CURSOR_KINDS = frozenset({"click_image", "click_at_relative"})

# How the single YAML key of a step maps onto its model's main field.
_SHORTHAND = {
    "hotkey": "keys", "wait": "seconds", "wait_for_image": "template", "click_image": "template",
    "scroll": "clicks", "assert_foreground": "foreground",
}


def _normalize_step(raw: Any) -> dict[str, Any]:
    """``{"hotkey": [f12]}`` -> ``{"kind": "hotkey", "keys": ["f12"]}`` and so on."""
    if not isinstance(raw, dict):
        raise ValueError("each step must be a mapping like {hotkey: [ctrl, s]}")
    kinds = [key for key in raw if key in _SHORTHAND or key in ("type", "click_at_relative")]
    if len(kinds) != 1:
        raise ValueError(f"step needs exactly one action key, got {sorted(raw)}")
    kind = kinds[0]
    value = raw[kind]
    options = {k: v for k, v in raw.items() if k != kind}
    step: dict[str, Any] = {"kind": kind, **options}
    if kind == "type":
        step.update(value if isinstance(value, dict) else {"text": value})
    elif kind == "click_at_relative":
        if isinstance(value, dict):
            step.update(value)
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            step.update({"x": value[0], "y": value[1]})
        else:
            raise ValueError("click_at_relative needs [x, y] or {x:, y:, units:}")
    else:
        step[_SHORTHAND[kind]] = value
    return step


class Macro(BaseModel):
    """A named sequence of GUI steps."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME)
    description: str = ""
    requires_foreground: ForegroundSpec | None = None
    cursor_wait_s: float = Field(1.0, ge=0, le=10)
    steps: list[Step] = Field(min_length=1, max_length=50)

    @field_validator("steps", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if not isinstance(value, list):
            raise ValueError("steps must be a list")
        out = []
        for index, raw in enumerate(value, 1):
            try:
                out.append(_normalize_step(raw))
            except ValueError as exc:
                raise ValueError(f"step {index}: {exc}") from None
        return out

    @property
    def uses_cursor(self) -> bool:
        return any(step.kind in CURSOR_KINDS for step in self.steps)


def describe_step(step: Any) -> str:
    """Short human label for logs and failure messages (no typed text)."""
    if isinstance(step, HotkeyStep):
        return f"hotkey {'+'.join(step.keys)}"
    if isinstance(step, (WaitForImageStep, ClickImageStep)):
        return f"{step.kind} {step.template}"
    if isinstance(step, TypeStep):
        return "type output path" if step.output_path else "type text"
    return step.kind


class MacroLibrary:
    """All macros from a folder of YAML files. Invalid files are reported, valid ones still load."""

    def __init__(self, macros: dict[str, Macro] | None = None, errors: dict[str, str] | None = None) -> None:
        self.macros = macros or {}
        self.errors = errors or {}

    @classmethod
    def from_dir(cls, directory: str | Path = DEFAULT_MACROS_DIR) -> MacroLibrary:
        library = cls()
        directory = Path(directory)
        if not directory.is_dir():
            return library
        for path in sorted(directory.glob("*.y*ml")):
            try:
                library.add_yaml(path.read_text(encoding="utf-8"), source=path.name)
            except (OSError, MacroError) as exc:
                library.errors[path.name] = str(exc)
                log.warning("macro file %s rejected: %s", path.name, exc)
        return library

    def add_yaml(self, text: str, source: str = "<string>") -> list[str]:
        """Parse one YAML document holding a macro or ``macros: [...]``. Returns the names added."""
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise MacroError(f"{source}: invalid YAML: {exc}") from exc
        items = data.get("macros") if isinstance(data, dict) and "macros" in data else [data]
        if not isinstance(items, list):
            raise MacroError(f"{source}: 'macros' must be a list")
        parsed = []
        for item in items:
            try:
                macro = Macro.model_validate(item)
            except ValidationError as exc:
                name = item.get("name", "?") if isinstance(item, dict) else "?"
                first = exc.errors()[0]
                where = ".".join(str(p) for p in first["loc"])
                raise MacroError(f"{source}: macro {name}: {where}: {first['msg']}") from None
            if macro.name in self.macros:
                raise MacroError(f"{source}: duplicate macro name {macro.name!r}")
            parsed.append(macro)
        for macro in parsed:
            self.macros[macro.name] = macro
        return [m.name for m in parsed]

    def get(self, name: str) -> Macro:
        if name in self.macros:
            return self.macros[name]
        raise MacroError(f"unknown macro {name!r}")

    def __contains__(self, name: object) -> bool:
        return name in self.macros


# ------------------------------------------------------------------ runner


class GuiActions(Protocol):
    """Input primitives the runner drives (the GuiAdapter, or a fake in tests)."""

    def hotkey(self, keys: Sequence[str]) -> None: ...
    def type_text(self, text: str) -> None: ...
    def click_at(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None: ...
    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> None: ...
    def move(self, x: int, y: int) -> None: ...
    def position(self) -> tuple[int, int]: ...


@dataclass
class MacroOutcome:
    """Result of one macro run. ``failed_step`` is 1-based (None if it failed before step 1)."""

    status: Status
    message: str
    failed_step: int | None = None
    steps_run: int = 0
    data: dict[str, Any] = field(default_factory=dict)


class _StepFailed(Exception):
    pass


def elevated_message(info: ForegroundInfo) -> str:
    """HUD text for PRD ACT-7: input cannot reach an elevated window."""
    app = info.process or "the foreground app"
    return f"Can't control {app}: it runs as administrator. Run AGC as administrator to control it."


class MacroRunner:
    """Plays macros through :class:`GuiActions`. Run it only on the GUI worker thread."""

    def __init__(self, actions: GuiActions, locator: ScreenLocator, probe: ForegroundProbe,
                 cursor: CursorArbiter, *, output_dir: str | Path = DEFAULT_OUTPUT_DIR,
                 self_elevated: bool = False) -> None:
        self.actions = actions
        self.locator = locator
        self.probe = probe
        self.cursor = cursor
        self.output_dir = output_dir
        self.self_elevated = self_elevated

    def run(self, macro: Macro, token: CancelToken) -> MacroOutcome:
        """Run every step or stop at the first failure; never raises."""
        index = 0
        try:
            token.raise_if_cancelled()
            self._check_window(macro, before_step=None)
            if macro.uses_cursor and not self.cursor.acquire(CURSOR_OWNER, macro.cursor_wait_s):
                why = "Pointer mode is using the cursor" if self.cursor.pointer_active else "the cursor is busy"
                return MacroOutcome(Status.FAILED, f"{macro.name}: {why}")
            try:
                home = self.actions.position() if macro.uses_cursor else None
                for index, step in enumerate(macro.steps, 1):
                    token.raise_if_cancelled()
                    self._check_window(macro, before_step=index)
                    if step.kind in CURSOR_KINDS and self.cursor.pointer_active:
                        raise _StepFailed("Pointer mode took over the cursor")
                    self._run_step(step, token)
                    if step.kind in CURSOR_KINDS and home is not None:
                        self.actions.move(*home)  # put the user's cursor back
            finally:
                if macro.uses_cursor:
                    self.cursor.release(CURSOR_OWNER)
        except CancelledError as exc:
            status = Status.TIMEOUT if exc.reason == "timeout" else Status.CANCELLED
            return MacroOutcome(status, f"{macro.name}: {exc.reason}", index or None, max(index - 1, 0))
        except _StepFailed as exc:
            where = f"step {index} ({describe_step(macro.steps[index - 1])})" if index else "before start"
            return MacroOutcome(Status.FAILED, f"{macro.name} {where}: {exc}", index or None, max(index - 1, 0))
        except (LocatorError, OutputPathError) as exc:
            return MacroOutcome(Status.FAILED, f"{macro.name} step {index}: {exc}", index or None,
                                max(index - 1, 0))
        return MacroOutcome(Status.OK, f"Ran {macro.name}", None, len(macro.steps))

    # ------------------------------------------------------------- helpers

    def _check_window(self, macro: Macro, before_step: int | None) -> None:
        info = self.probe.current()
        spec = macro.requires_foreground
        if spec is not None and not spec.matches(info):
            seen = info.process if info and info.process else "unknown window"
            verb = "is" if before_step is None else "changed to"
            raise _StepFailed(f"foreground {verb} {seen}, macro needs {spec.describe()}")
        if info is not None and info.elevated and not self.self_elevated:
            raise _StepFailed(elevated_message(info))

    def _run_step(self, step: Any, token: CancelToken) -> None:
        if isinstance(step, HotkeyStep):
            self.actions.hotkey(step.keys)
        elif isinstance(step, TypeStep):
            self.actions.type_text(step.text if step.text is not None else self._output_path(step.output_path))
        elif isinstance(step, WaitStep):
            token.wait(step.seconds)
            token.raise_if_cancelled()
        elif isinstance(step, WaitForImageStep):
            if self._find(step, token) is None:
                raise _StepFailed(f"{step.template} did not appear within {step.timeout:g}s")
        elif isinstance(step, ClickImageStep):
            found = self._find(step, token)
            if found is None:
                raise _StepFailed(f"{step.template} not found on screen within {step.timeout:g}s")
            self.actions.click_at(found.x + step.offset[0], found.y + step.offset[1], step.button, step.clicks)
        elif isinstance(step, ClickAtRelativeStep):
            info = self.probe.current()
            if info is None or info.rect is None:
                raise _StepFailed("foreground window position is unknown")
            left, top, width, height = info.rect
            dx, dy = (step.x * width, step.y * height) if step.units == "fraction" else (step.x, step.y)
            self.actions.click_at(int(left + dx), int(top + dy), step.button, step.clicks)
        elif isinstance(step, ScrollStep):
            self.actions.scroll(step.clicks)
        elif isinstance(step, AssertForegroundStep):
            info = self.probe.current()
            if not step.foreground.matches(info):
                seen = info.process if info and info.process else "unknown window"
                raise _StepFailed(f"foreground is {seen}, expected {step.foreground.describe()}")
        else:  # pragma: no cover - schema prevents this
            raise _StepFailed(f"unsupported step {step!r}")

    def _find(self, step: WaitForImageStep | ClickImageStep, token: CancelToken):
        return self.locator.find_on_screen(step.template, region=step.region, confidence=step.confidence,
                                           timeout_s=step.timeout, cancel_token=token)

    def _output_path(self, spec: OutputPathSpec | None) -> str:
        assert spec is not None
        directory = resolve_output_dir(self.output_dir, spec.out_dir)
        return str(next_free_path(directory, render_name(spec.name_template), spec.suffix))
