#!/usr/bin/env python3

import cv2
import numpy as np

# Create a blank image
img = np.zeros((200, 200, 3), dtype=np.uint8)
cv2.imshow("Key Code Tester", img)

while True:
    key = cv2.waitKey(0)
    print("Pressed key code:", key)
    if key == ord('q'):
        break

cv2.destroyAllWindows()
