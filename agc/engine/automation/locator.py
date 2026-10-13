"""Find a template image on screen with OpenCV (grayscale, multi-scale ``matchTemplate``).

Coordinates are physical screen pixels across the whole virtual desktop
(multi-monitor; left/top may be negative), matching what the input backend
uses once the process is DPI aware. Screenshots stay in memory and are never
written to disk. Templates live in ``agc/assets/gui_templates/``.

OpenCV and NumPy are imported lazily so the automation package stays light.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .cancel import CancelToken
from .win32_util import ensure_dpi_aware, virtual_screen_rect

if TYPE_CHECKING:
    import numpy as np

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "assets" / "gui_templates"
DEFAULT_SCALES = (0.9, 1.0, 1.1)

Region = tuple[int, int, int, int]  # left, top, width, height
#: Returns ``(grayscale image, (origin_x, origin_y))`` for a screen region (None = whole virtual desktop).
Grabber = Callable[[Region | None], "tuple[np.ndarray, tuple[int, int]]"]


class LocatorError(ValueError):
    """Bad template (missing, unreadable, outside the template folder, no detail)."""


@dataclass(frozen=True)
class Match:
    """Where a template was found: centre in screen pixels, score in [0, 1], and the scale used."""

    x: int
    y: int
    score: float
    scale: float
    width: int
    height: int


def match_template(haystack: np.ndarray, template: np.ndarray, *, confidence: float = 0.85,
                   scales: Sequence[float] = DEFAULT_SCALES,
                   origin: tuple[int, int] = (0, 0)) -> Match | None:
    """Best match of ``template`` in ``haystack`` (both grayscale uint8) over ``scales``, or None.

    A scale ``s`` resizes the template by ``s``: use it when the screen renders
    the target ``s`` times larger than when the template was captured.
    """
    import cv2
    import numpy as np

    if float(np.std(template)) < 1.0:
        raise LocatorError("template has no detail (a flat image matches everywhere)")
    best: Match | None = None
    hh, hw = haystack.shape[:2]
    for scale in scales:
        if scale == 1.0:
            scaled = template
        else:
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            scaled = cv2.resize(template, None, fx=scale, fy=scale, interpolation=interp)
        th, tw = scaled.shape[:2]
        if th < 4 or tw < 4 or th > hh or tw > hw:
            continue
        result = cv2.matchTemplate(haystack, scaled, cv2.TM_CCOEFF_NORMED)
        _, score, _, (left, top) = cv2.minMaxLoc(result)
        if not np.isfinite(score):
            continue
        if best is None or score > best.score:
            best = Match(x=origin[0] + left + tw // 2, y=origin[1] + top + th // 2,
                         score=float(score), scale=scale, width=tw, height=th)
    if best is None or best.score < confidence:
        return None
    return best


def grab_screen(region: Region | None = None) -> tuple[np.ndarray, tuple[int, int]]:
    """Grayscale screenshot of ``region`` (default: the whole virtual desktop) and its origin."""
    import cv2
    import numpy as np
    from PIL import ImageGrab

    ensure_dpi_aware()
    if region is None:
        region = virtual_screen_rect()
        if region[2] <= 0:  # non-Windows: let PIL decide
            image = ImageGrab.grab()
            return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY), (0, 0)
    left, top, width, height = region
    image = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY), (left, top)


class ScreenLocator:
    """Loads templates (cached) and polls the screen until one appears or the timeout passes."""

    def __init__(self, template_dir: str | Path = DEFAULT_TEMPLATE_DIR, grabber: Grabber | None = None) -> None:
        self.template_dir = Path(template_dir)
        self._grab = grabber or grab_screen
        self._cache: dict[Path, Any] = {}

    def template_path(self, name: str) -> Path:
        """Resolve a template name inside the template folder (no traversal)."""
        root = self.template_dir.resolve()
        path = (root / name).resolve()
        if root not in path.parents:
            raise LocatorError(f"template {name!r} must be inside {root}")
        return path

    def load_template(self, name: str) -> np.ndarray:
        """Grayscale template image (cached)."""
        import cv2
        import numpy as np

        path = self.template_path(name)
        if path not in self._cache:
            if not path.is_file():
                raise LocatorError(f"template {name!r} not found in {self.template_dir} "
                                   "(capture it with: python -m agc.tools.capture_template)")
            # imdecode(fromfile) handles non-ASCII paths, unlike imread on Windows.
            image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise LocatorError(f"template {name!r} is not a readable image")
            self._cache[path] = image
        return self._cache[path]

    def find_on_screen(self, template: str, region: Region | None = None, confidence: float = 0.85,
                       scales: Sequence[float] = DEFAULT_SCALES, timeout_s: float = 0.0,
                       cancel_token: CancelToken | None = None, poll_s: float = 0.1) -> Match | None:
        """Look for ``template`` until found or ``timeout_s`` passes (0 = one look). None if not found."""
        image = self.load_template(template)
        deadline = time.monotonic() + timeout_s
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            haystack, origin = self._grab(region)
            found = match_template(haystack, image, confidence=confidence, scales=scales, origin=origin)
            if found is not None or time.monotonic() >= deadline:
                return found
            if cancel_token is not None:
                cancel_token.wait(poll_s)
            else:
                time.sleep(poll_s)
