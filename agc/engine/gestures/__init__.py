"""Gesture detectors (PRD B5). Pure Python on landmark coordinates; no camera or OS access."""

from .static import PoseHoldDetector, finger_extended, finger_folded, is_peace_pose

__all__ = ["PoseHoldDetector", "finger_extended", "finger_folded", "is_peace_pose"]
