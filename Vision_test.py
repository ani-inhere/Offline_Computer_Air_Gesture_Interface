import cv2
import mediapipe as mp
import time  
import math  
import atexit

base_options = mp.tasks.BaseOptions(model_asset_path='hand_landmarker.task')

counter = 0

options = mp.tasks.vision.HandLandmarkerOptions(
    base_options=base_options,
    num_hands=2,
    min_hand_detection_confidence=0.3,
    min_hand_presence_confidence=0.3,
    min_tracking_confidence=0.3,
    running_mode=mp.tasks.vision.RunningMode.VIDEO 
)

detector = mp.tasks.vision.HandLandmarker.create_from_options(options)
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

print("Starting Optimized Tracker... Press 'q' to exit.")

# Gesture -> Word bridge (agc/engine/gesture_bridge.py; bindings in agc/profiles/word_workflow.yaml).
# Word runs on its own worker thread: the loop only calls bridge.on_gesture(), which never blocks.
try:
    from agc.engine.gesture_bridge import GestureActionBridge
except ImportError as exc:  # automation deps (pydantic, PyYAML, pywin32) not installed
    print(f"Automation disabled ({exc}); gestures will only be printed.")
    bridge = None
else:
    bridge = GestureActionBridge(on_status=lambda line: print(f"    [action] {line}"))
    bridge.start()
    atexit.register(bridge.shutdown)

TAP_MAX_MS = 300       # pinch released within this -> a tap
DOUBLE_TAP_MS = 500    # second tap within this of the first -> DOUBLE_TAP
LONG_PINCH_MS = 800    # pinch held this long -> LONG_PINCH
PEACE_HOLD_MS = 500    # peace sign (index + middle up in a V) held this long -> PEACE_HOLD

# Static pose detector (agc/engine/gestures/static.py): fires once per hold, re-arms after the pose is released.
from agc.engine.gestures.static import PoseHoldDetector, is_peace_pose
peace_detector = PoseHoldDetector("PEACE_HOLD", is_peace_pose, hold_ms=PEACE_HOLD_MS)


def emit_gesture(name):
    print(f'*** {name} detected! ***')
    if bridge is not None:
        bridge.on_gesture(name)


is_pinched = False
pinch_frames = 0
pinch_start_time = 0
last_tap_time = 0
tap_count = 0
long_pinch_fired = False

while cap.isOpened():
    success, frame = cap.read()
    if not success:
        continue

    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
    timestamp_ms = int(time.time() * 1000)
    results = detector.detect_for_video(mp_image, timestamp_ms)
    if results.hand_landmarks:
        height, width, _ = frame.shape
        
        for hand in results.hand_landmarks:
            for landmark in hand:
                pixel_x = int(landmark.x * width)
                pixel_y = int(landmark.y * height)
                cv2.circle(frame, (pixel_x, pixel_y), 5, (0, 0, 255), -1)
    L0 = L4 = L8 = L9 = None

    if results.hand_landmarks:
        hand_2d = results.hand_landmarks[0]
        L0 = (hand_2d[0].x, hand_2d[0].y)
        L4 = (hand_2d[4].x, hand_2d[4].y)
        L8 = (hand_2d[8].x, hand_2d[8].y)
        L9 = (hand_2d[9].x, hand_2d[9].y)
    def dist2d(a, b):
        return math.sqrt((b[0]-a[0])**2 + (b[1]-a[1])**2)
    if L0 is not None:
        s = dist2d(L0, L9)
        d_pinch = dist2d(L4, L8)
        pinch_ratio = d_pinch / s
        if pinch_ratio < 0.25:
            pinch_frames += 1
        if pinch_frames >= 2 and not is_pinched:
            is_pinched = True
            pinch_start_time = timestamp_ms
            long_pinch_fired = False
        # LONG_PINCH fires once while the pinch is still held, and cancels any pending tap.
        if is_pinched and not long_pinch_fired and (timestamp_ms - pinch_start_time) >= LONG_PINCH_MS:
            long_pinch_fired = True
            tap_count = 0
            emit_gesture("LONG_PINCH")
        if pinch_ratio > 0.40:
            if is_pinched and not long_pinch_fired and (timestamp_ms - pinch_start_time) <= TAP_MAX_MS:
                if tap_count == 1 and (timestamp_ms - last_tap_time) <= DOUBLE_TAP_MS:
                    tap_count = 0
                    emit_gesture("DOUBLE_TAP")
                else:
                    tap_count = 1
                    last_tap_time = timestamp_ms
                    
            is_pinched = False
            pinch_frames = 0

        # print(f"Pinch Ratio: {pinch_ratio:.4f}  (d_pinch={d_pinch:.4f}, s={s:.4f})")
    else:
        # Hand lost: drop a half-finished pinch so it can't turn into a LONG_PINCH later.
        is_pinched = False
        pinch_frames = 0

    # Peace sign held PEACE_HOLD_MS -> PEACE_HOLD (bound to opening a browser tab in the profile).
    hand_points = [(p.x, p.y) for p in results.hand_landmarks[0]] if results.hand_landmarks else None
    if peace_detector.update(hand_points, timestamp_ms):
        emit_gesture("PEACE_HOLD")

    # A tap only counts as TAP once the double-tap window has passed without a second tap.
    if tap_count == 1 and (timestamp_ms - last_tap_time) > DOUBLE_TAP_MS:
        tap_count = 0
        emit_gesture("TAP")

    if bridge is not None:
        cv2.putText(frame, bridge.status[:70], (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)

    cv2.imshow("Offline Hand Tracking", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()