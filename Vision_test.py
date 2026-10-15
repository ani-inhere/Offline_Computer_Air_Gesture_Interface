import cv2
import mediapipe as mp
import time  
import math  

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
is_pinched = False
pinch_frames = 0
pinch_start_time = 0
last_tap_time = 0
tap_count = 0

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
        if pinch_ratio > 0.40:
            if is_pinched and (timestamp_ms - pinch_start_time) <= 300:
                if (timestamp_ms - last_tap_time) <= 500: 
                    tap_count += 1
                else:
                    tap_count = 1
                
                last_tap_time = timestamp_ms
                
                if tap_count == 2:
                    print('*** DOUBLE TAP detected! ***')
                    tap_count = 0  
                else:
                    print('*** TAP detected! ***')
                    
            is_pinched = False
            pinch_frames = 0

        # print(f"Pinch Ratio: {pinch_ratio:.4f}  (d_pinch={d_pinch:.4f}, s={s:.4f})")

    cv2.imshow("Offline Hand Tracking", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()