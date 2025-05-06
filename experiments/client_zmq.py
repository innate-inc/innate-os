import zmq
import cv2
import numpy as np
import time

# match dims and type from server
H, W = 480, 640
VECTOR_LEN = 100
encode_before_loop = True  # Set to False to encode inside the loop
use_complex_image = False  # Set to True to use random noise images

ctx = zmq.Context()
sock = ctx.socket(zmq.REQ)
sock.connect("tcp://34.1.29.95:5555")

# dummy images
if use_complex_image:
    print("Using complex (random noise) images")
    img1 = np.random.randint(0, 256, (H, W, 3), dtype=np.uint8)
    img2 = np.random.randint(0, 256, (H, W, 3), dtype=np.uint8)
else:
    print("Using simple (solid gray) images")
    img1 = np.full((H, W, 3), 96, np.uint8)
    img2 = np.full((H, W, 3), 160, np.uint8)

vec = np.arange(VECTOR_LEN, dtype=np.int32)
vec_bytes = vec.tobytes()  # Convert vector to bytes once

# --- JPEG-encode once; change quality if needed -------------------------------
ENC_Q = 90  # 60–90 is typical; lower → smaller
jpg1_bytes = None
jpg2_bytes = None

if encode_before_loop:
    print("Shape of img1:", img1.shape)
    print("Shape of img2:", img2.shape)
    _, jpg1 = cv2.imencode(".jpg", img1, [cv2.IMWRITE_JPEG_QUALITY, ENC_Q])
    _, jpg2 = cv2.imencode(".jpg", img2, [cv2.IMWRITE_JPEG_QUALITY, ENC_Q])
    jpg1_bytes = jpg1.tobytes()
    jpg2_bytes = jpg2.tobytes()
    print(f"Encoding BEFORE loop: jpg1 {len(jpg1_bytes)} B   jpg2 {len(jpg2_bytes)} B")
    print(f"  (raw pair = {img1.nbytes*2} B)")
    msg_parts = [
        jpg1_bytes,
        jpg2_bytes,
        vec_bytes,
    ]  # Pre-assemble message parts if encoding before loop
else:
    print(f"Encoding INSIDE loop (raw pair = {img1.nbytes*2} B)")
    # If encoding inside loop, msg_parts will be assembled dynamically

# optional warm-up
# Prepare message based on encoding strategy for warm-up
if encode_before_loop:
    warmup_msg = msg_parts
else:

    _, jpg1 = cv2.imencode(".jpg", img1, [cv2.IMWRITE_JPEG_QUALITY, ENC_Q])
    _, jpg2 = cv2.imencode(".jpg", img2, [cv2.IMWRITE_JPEG_QUALITY, ENC_Q])
    warmup_msg = [jpg1.tobytes(), jpg2.tobytes(), vec_bytes]

sock.send_multipart(warmup_msg, copy=False)
sock.recv_multipart()

# timed trials
n_trials = 50  # Set the number of trials
times = []
for _ in range(n_trials):
    t0 = time.perf_counter()

    if encode_before_loop:
        # Send pre-encoded message
        sock.send_multipart(msg_parts, copy=False)
    else:
        # Encode and send message inside the loop
        _, jpg1 = cv2.imencode(".jpg", img1, [cv2.IMWRITE_JPEG_QUALITY, ENC_Q])
        _, jpg2 = cv2.imencode(".jpg", img2, [cv2.IMWRITE_JPEG_QUALITY, ENC_Q])
        msg = [jpg1.tobytes(), jpg2.tobytes(), vec_bytes]
        sock.send_multipart(msg, copy=False)  # copy=False = zero-copy into libzmq

    reply_parts = sock.recv_multipart()
    t1 = time.perf_counter()
    times.append((t1 - t0) * 1000)
    # Optional: print individual RTT if needed
    # print(f"RTT {(t1-t0)*1e3 :.2f} ms")
    # print("reply vec len:", len(reply_parts[0])) # Example: print len of first part

avg = sum(times) / len(times)
print(f"\nEncoding {'BEFORE' if encode_before_loop else 'INSIDE'} loop:")
print(f"Average round-trip: {avg:.3f} ms over {n_trials} trials")
