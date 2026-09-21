import cv2
import mediapipe as mp
import time  # NEW: We need time to generate timestamps
import math  # For Euclidean distance calculations

base_options = mp.tasks.BaseOptions(model_asset_path='hand_landmarker.task')

options = mp.tasks.vision.HandLandmarkerOptions(
    base_options=base_options,
    num_hands=2,
    min_hand_detection_confidence=0.3,
    min_hand_presence_confidence=0.3,
    min_tracking_confidence=0.3,
    running_mode=mp.tasks.vision.RunningMode.VIDEO # NEW: Enable Video Mode
)

detector = mp.tasks.vision.HandLandmarker.create_from_options(options)
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

print("Starting Optimized Tracker... Press 'q' to exit.")

# TAP micro-gesture state machine ─ initialise ONCE before the loop
is_pinched = False       # True while a confirmed pinch is held
pinch_frames = 0         # Consecutive frames below the pinch threshold
pinch_start_time = 0     # Timestamp (ms) when the pinch was first confirmed

while cap.isOpened():
    success, frame = cap.read()
    if not success:
        continue

    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

    # NEW: Calculate the current time in milliseconds
    timestamp_ms = int(time.time() * 1000)

    # NEW: Use the video detection function and pass the timestamp
    results = detector.detect_for_video(mp_image, timestamp_ms)

    # ... (Keep all your drawing code exactly the same below this)

    # 5. Extract coordinates and draw them manually
    if results.hand_landmarks:
        # Get frame dimensions to convert AI percentages into real pixel coordinates
        height, width, _ = frame.shape
        
        for hand in results.hand_landmarks:
            for landmark in hand:
                # The AI outputs x and y as decimals (e.g., 0.5 means middle of screen).
                # Multiply by width/height to get exact pixel locations.
                pixel_x = int(landmark.x * width)
                pixel_y = int(landmark.y * height)
                
                # Draw a solid red dot at every detected joint
                cv2.circle(frame, (pixel_x, pixel_y), 5, (0, 0, 255), -1)

    # 6. Extract 2D normalised landmarks for downstream geometry calculations.
    #    hand_landmarks x/y are normalised [0,1] screen coordinates.
    #    We intentionally ignore Z to avoid the depth-estimation spikes that
    #    occur when fingers occlude each other during a pinch.
    L0 = L4 = L8 = L9 = None  # initialise; remain None when no hand is detected

    if results.hand_landmarks:
        # Use the first detected hand (index 0).
        hand_2d = results.hand_landmarks[0]

        # Index 0  – Wrist
        L0 = (hand_2d[0].x, hand_2d[0].y)

        # Index 4  – Thumb Tip
        L4 = (hand_2d[4].x, hand_2d[4].y)

        # Index 8  – Index Finger Tip
        L8 = (hand_2d[8].x, hand_2d[8].y)

        # Index 9  – Middle Finger MCP (knuckle)
        L9 = (hand_2d[9].x, hand_2d[9].y)

    # L0, L4, L8, L9 are now (x, y) tuples in normalised screen coords, ready for geometry.

    # 7. IDP Feature Extraction ─────────────────────────────────────────────
    #
    #   Helper: 2D Euclidean distance between two (x, y) tuples.
    #   Formula: dist = sqrt((x₂-x₁)² + (y₂-y₁)²)
    def dist2d(a, b):
        return math.sqrt((b[0]-a[0])**2 + (b[1]-a[1])**2)

    if L0 is not None:
        # Hand Scale (s): Wrist → Middle MCP distance.
        # Acts as a normalisation factor so the pinch ratio is invariant to
        # how close the hand is to the camera.
        s = dist2d(L0, L9)

        # Raw Thumb–Index distance (d_pinch): Thumb Tip → Index Tip.
        d_pinch = dist2d(L4, L8)

        # Pinch Ratio: normalise d_pinch by Hand Scale s.
        # → 0.0  hand fully pinched
        # → ~1.0 fingers spread apart (ratio relative to hand size)
        pinch_ratio = d_pinch / s

        # ── TAP hysteresis state machine ──────────────────────────────────
        if pinch_ratio < 0.25:
            pinch_frames += 1

        if pinch_frames >= 2 and not is_pinched:
            is_pinched = True
            pinch_start_time = timestamp_ms

        if pinch_ratio > 0.40:
            if is_pinched and (timestamp_ms - pinch_start_time) <= 300:
                print('*** TAP detected! ***')
            is_pinched = False
            pinch_frames = 0
        # ──────────────────────────────────────────────────────────────────

        print(f"Pinch Ratio: {pinch_ratio:.4f}  (d_pinch={d_pinch:.4f}, s={s:.4f})")

    cv2.imshow("Offline Hand Tracking", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()