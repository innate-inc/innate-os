# innate_uninavid — WebSocket Protocol

## Overview

The `uninavid_node` opens a **plain (no-auth) WebSocket client** connection to the URL in `UNINAVID_WS_URL` (`.env`).

Two data flows run over the same connection:

| Direction | Content | Rate |
|-----------|---------|------|
| Robot → Server | Compressed camera frame | `IMAGE_SEND_HZ` (default 1 Hz) |
| Server → Robot | Integer action code | Whenever the server decides |

---

## Robot → Server (camera frame)

Each message is a **binary WebSocket frame** with the structure:

```
<JSON header>\n<raw image bytes>
```

### JSON header fields

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `type` | string | `"image"` | Always `"image"` |
| `format` | string | `"jpeg"` | Encoding reported by ROS (`CompressedImage.format`) |
| `stamp_sec` | int | `1741152778` | ROS stamp seconds |
| `stamp_nanosec` | int | `482000000` | ROS stamp nanoseconds |

### Example (pseudo-bytes)

```
{"type": "image", "format": "jpeg", "stamp_sec": 1741152778, "stamp_nanosec": 482000000}\n\xff\xd8\xff...
```

The first `\n` is the delimiter; everything after it is the raw compressed image data.

---

## Server → Robot (action command)

Each message is a **text WebSocket frame** containing a single integer string.

### Action codes

| Code | Constant | linear.x (m/s) | angular.z (rad/s) |
|------|----------|---------------:|------------------:|
| `0` | `ACTION_STOP` | 0.0 | 0.0 |
| `1` | `ACTION_FORWARD` | 0.3 | 0.0 |
| `2` | `ACTION_LEFT` | 0.0 | +0.8 |
| `3` | `ACTION_RIGHT` | 0.0 | −0.8 |

### Example exchange

```
server  →  "1"      # robot moves forward
server  →  "2"      # robot turns left
server  →  "0"      # robot stops
```

The node publishes the resulting `geometry_msgs/Twist` on `/cmd_vel` immediately on receipt.  
Unknown codes are silently ignored (no publish, no disconnect).

---

## Connection behaviour

- The node **auto-reconnects** with a 2-second back-off on any disconnection or error.
- There is **no authentication** — connect directly to `UNINAVID_WS_URL`.
- The image send loop and command receive loop run **concurrently** on the same connection.
