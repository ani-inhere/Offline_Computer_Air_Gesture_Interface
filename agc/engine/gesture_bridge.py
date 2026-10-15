"""Bridge from vision gesture events to the automation backend.

The vision loop calls :meth:`GestureActionBridge.on_gesture` with a gesture name
(``"TAP"``, ``"DOUBLE_TAP"``, ``"LONG_PINCH"``). The bridge looks the gesture up
in a YAML profile, builds an :class:`ActionRequest` and hands it to
:meth:`ActionDispatcher.submit`, which never blocks, so the camera loop keeps
running while Word works. Results arrive on the dispatcher's notifier thread and
are kept in :attr:`GestureActionBridge.status` for the on-screen HUD.

This is the minimal mapper from the PRD, ``(gesture, profile) -> ActionRequest``.
The Phase 4 router (``backend: auto`` across tiers, ``gui_fallback``) is not built
yet; until then requests go to the tier named by ``backend`` or implied by ``type``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .automation import ActionDispatcher, ActionRequest, ActionResult, ActionType, Backend
from .automation.adapters.base import BaseAdapter
from .automation.interfaces import ForegroundProbe
from .automation.win32_util import default_foreground_probe

log = logging.getLogger("agc.gesture_bridge")

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[1] / "profiles" / "word_workflow.yaml"


# ------------------------------------------------------------------ profile schema


class BindingAction(BaseModel):
    """The ActionRequest fields a profile may set for one binding."""

    model_config = ConfigDict(extra="forbid")

    type: ActionType
    app: str | None = None
    op: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    backend: Backend = Backend.AUTO
    timeout_s: float = Field(default=5.0, gt=0, le=600)


class Binding(BaseModel):
    """One gesture bound to one action."""

    model_config = ConfigDict(extra="forbid")

    gesture: str = Field(min_length=1)
    action: BindingAction
    cooldown_s: float = Field(default=1.0, ge=0, description="Ignore repeats of this gesture for this long")


class ProfileMatch(BaseModel):
    """Foreground apps a profile applies to (process names, case-insensitive)."""

    model_config = ConfigDict(extra="forbid")

    process: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    """A set of bindings; ``match`` makes it app-specific, otherwise it is the default."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    match: ProfileMatch | None = None
    bindings: list[Binding] = Field(default_factory=list)

    def binding_for(self, gesture: str) -> Binding | None:
        """The first binding for ``gesture``, if any."""
        for binding in self.bindings:
            if binding.gesture == gesture:
                return binding
        return None


class ProfileFile(BaseModel):
    """Top level of a profile YAML file."""

    model_config = ConfigDict(extra="forbid")

    word: dict[str, Any] = Field(default_factory=dict, description="WordConfig overrides")
    browser: dict[str, Any] = Field(default_factory=dict, description="BrowserConfig overrides")
    profiles: list[Profile] = Field(min_length=1)


def load_profile_file(path: str | Path) -> ProfileFile:
    """Read and validate a profile YAML file. Raises ``ValueError`` naming the file on any problem."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ProfileFile.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"invalid profile file {path}: {exc}") from exc


def default_adapters(word_overrides: dict[str, Any] | None = None,
                     browser_overrides: dict[str, Any] | None = None) -> list[BaseAdapter]:
    """The production adapters: Word over COM and the browser (Tier 2), PyAutoGUI (Tier 3).

    All import their platform libraries lazily and report themselves unavailable
    when pywin32 / PyAutoGUI / Word / the browser are missing.
    """
    from .automation.adapters.browser import BrowserAdapter, BrowserConfig
    from .automation.adapters.gui_pyautogui import GuiAutomationAdapter
    from .automation.adapters.word_com import WordAdapter, WordConfig

    return [WordAdapter(WordConfig(**(word_overrides or {}))),
            BrowserAdapter(BrowserConfig(**(browser_overrides or {}))),
            GuiAutomationAdapter()]


# ------------------------------------------------------------------------ bridge


class GestureActionBridge:
    """Maps gesture names to automation requests and submits them without blocking.

    Args:
        profile_path: profile YAML (default ``agc/profiles/word_workflow.yaml``).
        dispatcher: dispatcher to use; a new :class:`ActionDispatcher` by default.
        adapters: adapters to register; :func:`default_adapters` by default.
        probe: foreground-window probe for ``match`` profiles.
        clock: monotonic clock in seconds (tests).
        on_status: called with a one-line status after every result
            (on the dispatcher's notifier thread, never on the vision loop).
    """

    def __init__(
        self,
        profile_path: str | Path = DEFAULT_PROFILE_PATH,
        *,
        dispatcher: ActionDispatcher | None = None,
        adapters: Iterable[BaseAdapter] | None = None,
        probe: ForegroundProbe | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.profile_path = Path(profile_path)
        self.config = load_profile_file(self.profile_path)
        self._adapters = list(adapters) if adapters is not None else default_adapters(self.config.word, self.config.browser)
        self._validate_bindings()
        self._dispatcher = dispatcher or ActionDispatcher()
        self._dispatcher.set_result_callback(self._on_result)
        for adapter in self._adapters:
            self._dispatcher.register_backend(adapter.tier, adapter)
        self._probe = probe or default_foreground_probe()
        self._clock = clock
        self._on_status = on_status
        self._lock = threading.Lock()
        self._last_fired: dict[tuple[str, str], float] = {}
        self._pending: dict[str, str] = {}  # job id -> "GESTURE -> app.op"
        self._status = "Ready: " + ", ".join(self.gestures)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the dispatcher's worker threads. Idempotent."""
        self._dispatcher.start()

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Cancel pending jobs and stop the workers (Word is only quit if AGC started it). Idempotent."""
        return self._dispatcher.shutdown(timeout)

    # -------------------------------------------------------------- hot path

    def on_gesture(self, gesture: str) -> str | None:
        """Submit the action bound to ``gesture``. Never blocks.

        Returns the job id, or None when the gesture is unbound or still in cooldown.
        """
        profile, binding = self._resolve(gesture)
        if profile is None or binding is None:
            return None
        key = (profile.id, binding.gesture)
        now = self._clock()
        last = self._last_fired.get(key)
        if last is not None and now - last < binding.cooldown_s:
            return None
        self._last_fired[key] = now
        request = self.build_request(binding)
        target = f"{request.app}.{request.op}" if request.app else request.op
        label = f"{gesture} -> {target}"
        with self._lock:
            self._pending[request.id] = label  # before submit: the result may arrive immediately
            self._status = f"{label} ..."
        return self._dispatcher.submit(request)

    @staticmethod
    def build_request(binding: Binding) -> ActionRequest:
        """The ActionRequest for ``binding`` (a fresh id each call)."""
        action = binding.action
        return ActionRequest(
            type=action.type, app=action.app, op=action.op, args=dict(action.args),
            backend=action.backend, timeout_s=action.timeout_s,
        )

    # ------------------------------------------------------------ inspection

    @property
    def status(self) -> str:
        """Latest one-line status for the HUD."""
        with self._lock:
            return self._status

    @property
    def gestures(self) -> list[str]:
        """Every gesture name bound in any profile."""
        return sorted({b.gesture for p in self.config.profiles for b in p.bindings})

    @property
    def dispatcher(self) -> ActionDispatcher:
        return self._dispatcher

    # ------------------------------------------------------------- internals

    def _resolve(self, gesture: str) -> tuple[Profile | None, Binding | None]:
        """App-specific profile for the foreground process first, then the default profile(s)."""
        profiles = self.config.profiles
        if any(p.match for p in profiles):
            process = self._foreground_process()
            if process:
                for profile in profiles:
                    if profile.match and process in {name.lower() for name in profile.match.process}:
                        binding = profile.binding_for(gesture)
                        if binding is not None:
                            return profile, binding
        for profile in profiles:
            if profile.match is None:
                binding = profile.binding_for(gesture)
                if binding is not None:
                    return profile, binding
        return None, None

    def _foreground_process(self) -> str:
        try:
            info = self._probe.current()
        except Exception:  # noqa: BLE001 - context is best-effort; fall back to the default profile
            log.debug("foreground probe failed", exc_info=True)
            return ""
        return info.process.lower() if info is not None else ""

    def _validate_bindings(self) -> None:
        """Reject app_api bindings whose app has no adapter or whose op the adapter doesn't support."""
        for profile in self.config.profiles:
            for binding in profile.bindings:
                action = binding.action
                if action.type is not ActionType.APP_API:
                    continue
                adapter = next((a for a in self._adapters if getattr(a, "app", None) == action.app), None)
                where = f"{self.profile_path.name}: profile {profile.id!r}, gesture {binding.gesture!r}"
                if adapter is None:
                    raise ValueError(f"{where}: no adapter for app {action.app!r}")
                ops = getattr(adapter, "ops", None)
                if ops and action.op not in ops:
                    raise ValueError(f"{where}: {action.app} has no op {action.op!r} (known: {', '.join(sorted(ops))})")
                validate_args = getattr(adapter, "validate_args", None)  # e.g. the browser checks the URL
                if validate_args is not None:
                    try:
                        validate_args(action.op, action.args)
                    except ValueError as exc:
                        raise ValueError(f"{where}: {exc}") from None

    def _on_result(self, result: ActionResult) -> None:
        """Dispatcher notifier thread: record the outcome for the HUD."""
        with self._lock:
            label = self._pending.pop(result.id, result.id[:8])
            line = f"{label}: {result.status.value.upper()}"
            if result.message:
                line += f" - {result.message}"
            self._status = line
        log.info(line)
        if self._on_status is not None:
            try:
                self._on_status(line)
            except Exception:  # noqa: BLE001 - a broken UI callback must not kill the notifier
                log.exception("on_status callback failed")

