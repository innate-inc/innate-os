import v4l2capture
import select
from PIL import Image

# Create video device
video = v4l2capture.Video_device("/dev/video0")

# Get device info
print("Device info:", video.get_info())

# Set format (use MJPEG since that's what works)
size_x, size_y = video.set_format(640, 240, fourcc='MJPG')
print(f"Set format: {size_x}x{size_y}")

# Create buffer
video.create_buffers(1)
video.queue_all_buffers()

# Start capture
video.start()

# Capture frame
select.select((video,), (), ())
image_data = video.read_and_queue()

# Convert to image
image = Image.frombytes("RGB", (size_x, size_y), image_data)
image.save("v4l2_capture.jpg")

video.close()