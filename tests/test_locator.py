"""Template locator on synthetic screens: exact, scaled, noisy, absent, offsets, polling."""

from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np
import pytest
from fake_gui import distinctive_template, noise_screen, paste

from agc.engine.automation import CancelledError, CancelToken
from agc.engine.automation.locator import LocatorError, ScreenLocator, match_template


@pytest.fixture
def template() -> np.ndarray:
    return distinctive_template()


def save_png(path: Path, image: np.ndarray) -> None:
    ok, data = cv2.imencode(".png", image)
    assert ok
    data.tofile(str(path))


def test_exact_match_returns_centre(template):
    screen = paste(noise_screen(), template, left=200, top=120)
    found = match_template(screen, template)
    assert found is not None and found.score > 0.99
    assert (found.x, found.y) == (200 + 48 // 2, 120 + 30 // 2)
    assert found.scale == 1.0


@pytest.mark.parametrize("scale", [0.9, 1.1])
def test_scaled_target_found_at_matching_scale(template, scale):
    shown = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    screen = paste(noise_screen(), shown, left=300, top=200)
    found = match_template(screen, template, scales=(0.9, 1.0, 1.1))
    assert found is not None and found.scale == scale
    assert abs(found.x - (300 + shown.shape[1] // 2)) <= 2
    assert abs(found.y - (200 + shown.shape[0] // 2)) <= 2


def test_noisy_screen_still_matches(template):
    rng = np.random.default_rng(7)
    screen = paste(noise_screen(), template, left=50, top=300).astype(np.int16)
    screen = np.clip(screen + rng.normal(0, 12, screen.shape), 0, 255).astype(np.uint8)
    found = match_template(screen, template, confidence=0.85)
    assert found is not None and found.score >= 0.85
    assert (found.x, found.y) == (50 + 24, 300 + 15)


def test_absent_template_returns_none(template):
    assert match_template(noise_screen(), template, confidence=0.85) is None


def test_origin_offsets_for_multi_monitor(template):
    screen = paste(noise_screen(), template, left=10, top=20)
    found = match_template(screen, template, origin=(-1920, -100))  # monitor left of the primary
    assert (found.x, found.y) == (-1920 + 10 + 24, -100 + 20 + 15)


def test_flat_template_is_rejected():
    with pytest.raises(LocatorError, match="no detail"):
        match_template(noise_screen(), np.full((20, 20), 128, np.uint8))


def test_template_larger_than_screen_is_skipped(template):
    assert match_template(template[:10, :10].copy() + 1, template) is None


def test_find_on_screen_polls_until_target_appears(tmp_path, template):
    save_png(tmp_path / "button.png", template)
    frames = [noise_screen(), noise_screen(seed=3), paste(noise_screen(), template, 400, 40)]
    grabs = []

    def grabber(region):
        grabs.append(region)
        return frames[min(len(grabs) - 1, 2)], (0, 0)

    locator = ScreenLocator(tmp_path, grabber=grabber)
    found = locator.find_on_screen("button.png", timeout_s=2, poll_s=0.01)
    assert found is not None and (found.x, found.y) == (424, 55)
    assert len(grabs) == 3


def test_find_on_screen_passes_region_and_gives_up(tmp_path, template):
    save_png(tmp_path / "button.png", template)
    seen = []
    locator = ScreenLocator(tmp_path, grabber=lambda region: (seen.append(region), (noise_screen(), (0, 0)))[1])
    assert locator.find_on_screen("button.png", region=(10, 20, 300, 200), timeout_s=0.05, poll_s=0.01) is None
    assert seen and all(r == (10, 20, 300, 200) for r in seen)


def test_find_on_screen_stops_on_cancel(tmp_path, template):
    save_png(tmp_path / "button.png", template)
    locator = ScreenLocator(tmp_path, grabber=lambda region: (noise_screen(), (0, 0)))
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    with pytest.raises(CancelledError):
        locator.find_on_screen("button.png", timeout_s=10, cancel_token=token, poll_s=0.01)


def test_template_names_cannot_escape_folder(tmp_path):
    locator = ScreenLocator(tmp_path / "templates", grabber=lambda r: (noise_screen(), (0, 0)))
    with pytest.raises(LocatorError, match="must be inside"):
        locator.load_template("../secret.png")
    with pytest.raises(LocatorError, match="not found"):
        locator.load_template("missing.png")


def test_unreadable_template(tmp_path):
    (tmp_path / "bad.png").write_bytes(b"not a png")
    with pytest.raises(LocatorError, match="not a readable image"):
        ScreenLocator(tmp_path).load_template("bad.png")
