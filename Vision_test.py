import cv2
import mediapipe as mp
import time # NEW: We need time to generate timestamps

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

    cv2.imshow("Offline Hand Tracking", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()