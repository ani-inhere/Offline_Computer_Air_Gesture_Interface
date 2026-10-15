"""Static pose detection (peace sign) and the hold detector, on synthetic hand landmarks."""

from __future__ import annotations

import math

import pytest

from agc.engine.gestures.static import PoseHoldDetector, finger_extended, finger_folded, is_peace_pose

# MCP x-offsets from the wrist and a default pointing angle per finger (degrees; 0 = straight up).
_MCP = {"index": (-0.06, 0.0), "middle": (-0.02, 0.0), "ring": (0.02, 0.0), "pinky": (0.055, 0.0)}
_INDEX = {"index": 5, "middle": 9, "ring": 13, "pinky": 17}


def make_hand(extended=(), half_bent=(), angles=None, rotate_deg=0.0, scale=1.0, at=(0.5, 0.8)):
    """21 (x, y) landmarks of a hand pointing up the image (y decreases upward).

    Wrist to middle MCP is 0.2 units. ``angles`` tilts extended fingers (V-spread).
    """
    angles = angles or {}
    points = [(0.0, 0.0)] * 21
    for i, (x, y) in enumerate([(-0.05, -0.04), (-0.08, -0.08), (-0.10, -0.11), (-0.11, -0.14)], start=1):
        points[i] = (x, y)  # thumb, unused by the detectors
    for finger, base in _INDEX.items():
        mx = _MCP[finger][0]
        mcp = (mx, -0.2)
        if finger in extended:
            a = math.radians(angles.get(finger, 0.0))
            ux, uy = math.sin(a), -math.cos(a)
            seg = [0.09, 0.05, 0.04]
            pip = (mcp[0] + ux * seg[0], mcp[1] + uy * seg[0])
            dip = (pip[0] + ux * seg[1], pip[1] + uy * seg[1])
            tip = (dip[0] + ux * seg[2], dip[1] + uy * seg[2])
        elif finger in half_bent:
            pip = (mx, -0.27)
            dip = (mx, -0.31)
            tip = (mx, -0.29)  # tip roughly level with PIP: neither extended nor folded
        else:  # folded: curls back toward the palm
            pip = (mx, -0.27)
            dip = (mx, -0.24)
            tip = (mx, -0.18)
        points[base:base + 4] = [mcp, pip, dip, tip]
    r = math.radians(rotate_deg)
    cos_r, sin_r = math.cos(r), math.sin(r)
    return [(at[0] + scale * (x * cos_r - y * sin_r), at[1] + scale * (x * sin_r + y * cos_r)) for x, y in points]


PEACE = {"extended": ("index", "middle"), "angles": {"index": -15, "middle": 10}}


@pytest.mark.parametrize("rotate_deg", [0, 35, 90, 180, -60])
@pytest.mark.parametrize("scale", [0.5, 1.0, 1.8])
def test_peace_sign_detected_at_any_rotation_and_distance(rotate_deg, scale):
    assert is_peace_pose(make_hand(**PEACE, rotate_deg=rotate_deg, scale=scale))


@pytest.mark.parametrize("hand, why", [
    (dict(extended=("index", "middle")), "fingers together (two-finger scroll pose, PRD SCROLL_2F)"),
    (dict(extended=("index", "middle", "ring", "pinky")), "open palm"),
    (dict(), "fist"),
    (dict(extended=("index",)), "pointing"),
    (dict(extended=("index", "middle", "ring"), angles={"index": -15, "middle": 10}), "three fingers"),
    (dict(**PEACE, half_bent=("ring",)), "ring finger only half bent"),
])
def test_other_poses_are_not_peace(hand, why):
    assert not is_peace_pose(make_hand(**hand)), why


def test_finger_states_have_an_unsure_band():
    hand = make_hand(extended=("index",), half_bent=("ring",))
    assert finger_extended(hand, "index") and not finger_folded(hand, "index")
    assert finger_folded(hand, "pinky") and not finger_extended(hand, "pinky")
    assert not finger_extended(hand, "ring") and not finger_folded(hand, "ring")


def test_degenerate_input():
    assert not is_peace_pose([])
    assert not is_peace_pose([(0.5, 0.5)] * 21)


# ------------------------------------------------------------- hold detector


def feed(detector, frames):
    """frames: list of (pose_present, t_ms); pose_present None means no hand. Returns emitted times."""
    peace, other = make_hand(**PEACE), make_hand()
    fired = []
    for present, t in frames:
        hand = None if present is None else (peace if present else other)
        if detector.update(hand, t):
            fired.append(t)
    return fired


def every_33ms(start, end, present=True):
    return [(present, t) for t in range(start, end, 33)]


def test_fires_once_after_hold():
    detector = PoseHoldDetector("PEACE_HOLD", is_peace_pose, hold_ms=500)
    fired = feed(detector, every_33ms(0, 2000))
    assert fired == [528]  # first frame at or after 500 ms of holding; holding longer never repeats


def test_short_hold_does_not_fire():
    detector = PoseHoldDetector("PEACE_HOLD", is_peace_pose, hold_ms=500)
    assert feed(detector, every_33ms(0, 400) + every_33ms(400, 600, present=False)) == []


def test_single_frame_dropouts_do_not_reset_the_hold():
    detector = PoseHoldDetector("PEACE_HOLD", is_peace_pose, hold_ms=500, release_frames=3)
    frames = every_33ms(0, 300) + [(False, 300), (False, 333)] + every_33ms(366, 700)
    assert feed(detector, frames) == [531]  # hold kept its 0 ms start through 2 missed frames


def test_release_rearms_for_the_next_hold():
    detector = PoseHoldDetector("PEACE_HOLD", is_peace_pose, hold_ms=500, release_frames=3)
    frames = every_33ms(0, 700) + every_33ms(700, 800, present=False) + every_33ms(800, 1500)
    assert feed(detector, frames) == [528, 1328]


def test_losing_the_hand_resets_immediately():
    detector = PoseHoldDetector("PEACE_HOLD", is_peace_pose, hold_ms=500)
    frames = every_33ms(0, 400) + [(None, 400)] + every_33ms(433, 1100)
    assert feed(detector, frames) == [961]  # hold restarted at 433 ms


def test_progress_for_hud():
    detector = PoseHoldDetector("PEACE_HOLD", is_peace_pose, hold_ms=500)
    peace = make_hand(**PEACE)
    detector.update(peace, 0)
    detector.update(peace, 250)
    assert detector.progress(250) == pytest.approx(0.5) and detector.holding
    detector.update(peace, 500)
    assert detector.progress(500) == 1.0
