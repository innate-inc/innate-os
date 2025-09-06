import cv2

# Initialize camera (0 is usually the first camera)
cap = cv2.VideoCapture(0)

# Set resolution if needed
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 3840)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

# Capture frame
ret, frame = cap.read()
if ret:
    cv2.imwrite('captured_image.jpg', frame)
    print("Image saved!")
else:
    print("Failed to capture image")

cap.release()
