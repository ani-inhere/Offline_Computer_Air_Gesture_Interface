"""Static hand poses from MediaPipe landmarks, plus a hold detector (PRD B5, FSM-2/FSM-3).

Works on the 21 hand landmarks as ``(x, y)`` pairs (MediaPipe image
coordinates). Every test compares distances from the wrist, so poses are
recognised at any hand rotation or distance from the camera.

Landmark indices: 0 wrist; per finger MCP, PIP, DIP, TIP:
index 5-8, middle 9-12, ring 13-16, pinky 17-20 (thumb 1-4 is not used).
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

Point = Sequence[float]
Landmarks = Sequence[Point]

WRIST = 0
#: finger -> (PIP, TIP) landmark indices
FINGERS = {"index": (6, 8), "middle": (10, 12), "ring": (14, 16), "pinky": (18, 20)}

#: A finger is extended when its tip is this much farther from the wrist than its PIP joint...
EXTENDED_RATIO = 1.15
#: ...and folded when the tip is closer to the wrist than this. In-between is "unsure" (neither).
FOLDED_RATIO = 0.95
#: Peace sign: index and middle tips at least this far apart, in hand-scale units (wrist to middle MCP).
PEACE_MIN_SPREAD = 0.30


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _tip_ratio(landmarks: Landmarks, finger: str) -> float:
    pip, tip = FINGERS[finger]
    pip_dist = _dist(landmarks[WRIST], landmarks[pip])
    return _dist(landmarks[WRIST], landmarks[tip]) / pip_dist if pip_dist > 1e-6 else 0.0


def finger_extended(landmarks: Landmarks, finger: str) -> bool:
    """True if ``finger`` is clearly straight."""
    return _tip_ratio(landmarks, finger) > EXTENDED_RATIO


def finger_folded(landmarks: Landmarks, finger: str) -> bool:
    """True if ``finger`` is clearly curled toward the palm."""
    return _tip_ratio(landmarks, finger) < FOLDED_RATIO


def hand_scale(landmarks: Landmarks) -> float:
    """Wrist to middle-finger MCP (PRD B3.4 ``s``), in landmark units."""
    return _dist(landmarks[WRIST], landmarks[9])


def is_peace_pose(landmarks: Landmarks) -> bool:
    """Index and middle extended and spread in a V; ring and pinky folded. Thumb ignored."""
    if len(landmarks) < 21:
        return False
    s = hand_scale(landmarks)
    if s <= 1e-6:
        return False
    return (finger_extended(landmarks, "index") and finger_extended(landmarks, "middle")
            and finger_folded(landmarks, "ring") and finger_folded(landmarks, "pinky")
            and _dist(landmarks[8], landmarks[12]) / s >= PEACE_MIN_SPREAD)


class PoseHoldDetector:
    """Emits ``name`` once when a pose has been held for ``hold_ms``.

    It fires again only after the pose has been released for ``release_frames``
    consecutive frames (debounce, PRD FSM-3), so holding the pose does not
    repeat the action. Brief single-frame dropouts do not reset the hold.
    """

    def __init__(self, name: str, pose: Callable[[Landmarks], bool], hold_ms: int = 500,
                 release_frames: int = 3) -> None:
        self.name = name
        self._pose = pose
        self.hold_ms = hold_ms
        self.release_frames = release_frames
        self._start_ms: Optional[int] = None
        self._misses = 0
        self._fired = False

    @property
    def holding(self) -> bool:
        """True while the pose is being held (before or after it fired)."""
        return self._start_ms is not None

    def progress(self, now_ms: int) -> float:
        """0..1 progress toward firing (for a HUD dwell ring)."""
        if self._start_ms is None or self._fired:
            return 1.0 if self._fired else 0.0
        return min(1.0, (now_ms - self._start_ms) / self.hold_ms)

    def reset(self) -> None:
        self._start_ms = None
        self._misses = 0
        self._fired = False

    def update(self, landmarks: Optional[Landmarks], now_ms: int) -> Optional[str]:
        """Feed one frame (``None`` = no hand). Returns ``name`` on the frame the hold completes."""
        if landmarks is not None and self._pose(landmarks):
            self._misses = 0
            if self._start_ms is None:
                self._start_ms = now_ms
            if not self._fired and now_ms - self._start_ms >= self.hold_ms:
                self._fired = True
                return self.name
            return None
        self._misses += 1
        if landmarks is None or self._misses >= self.release_frames:
            self.reset()
        return None
