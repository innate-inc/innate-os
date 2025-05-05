import zmq
import numpy as np
import time


def mock_process(img1: np.ndarray, img2: np.ndarray, vec: np.ndarray) -> np.ndarray:
    # A stand-in for your real work:
    # e.g. blend the two images by averaging pixels, then append sum(vec)
    out = np.mean([img1, img2], axis=0).astype(np.uint8)
    # just return a vector of same size with all values = sum(vec) % 256
    return np.full_like(vec, int(np.sum(vec) % 256))


context = zmq.Context()
sock = context.socket(zmq.ROUTER)
sock.bind("tcp://0.0.0.0:5555")
print("Server listening on port 5555")

H = 480
W = 640

while True:
    parts = sock.recv_multipart()
    start = time.perf_counter()

    # ROUTER sockets prepend the client identity
    identity = parts[0]

    # Actual message payload starts from the second frame
    img1 = np.frombuffer(parts[1], dtype=np.uint8).reshape((H, W, 3))
    img2 = np.frombuffer(parts[2], dtype=np.uint8).reshape((H, W, 3))
    vec = np.frombuffer(parts[3], dtype=np.int32)

    out_vec = mock_process(img1, img2, vec)
    reply_payload = out_vec.tobytes()

    # Send reply back to the specific client using its identity
    # [identity, empty_delimiter, payload]
    sock.send_multipart([identity, b"", reply_payload])
    elapsed = (time.perf_counter() - start) * 1000
    print(f"Handled request in {elapsed:.3f} ms")
