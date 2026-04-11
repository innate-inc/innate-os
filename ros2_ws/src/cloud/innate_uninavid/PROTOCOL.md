# innate_uninavid — WebSocket Protocol

## Overview

The `uninavid_node` is a ROS 2 node that bridges a compressed camera feed to
the UniNavid cloud service over a WebSocket connection.  It exposes a
`navigate_instruction` action server (`innate_cloud_msgs/action/NavigateInstruction`).
When a goal is accepted, the node opens an **authenticated WebSocket** to the
configured URL, sends the natural-language instruction as the first text
message, then streams camera frames and receives action commands until the
goal completes, is cancelled, or an error occurs.

### Launch

```bash
ros2 launch innate_uninavid uninavid.launch.py
# Override the config file:
ros2 launch innate_uninavid uninavid.launch.py params_file:=/path/to/custom.yaml
```

The launch file applies a `/cmd_vel` → `/cmd_vel_scaled` remapping.

### ROS parameters

All tunables are declared as ROS parameters and can be set via the YAML
config file at `config/params.yaml` or overridden on the command line.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ws_url` | string | env `UNINAVID_WS_URL` or `wss://nav-v1.innate.bot` | WebSocket server URL |
| `service_key` | string | env `INNATE_SERVICE_KEY` | Service key for auth token acquisition |
| `auth_issuer_url` | string | env `INNATE_AUTH_URL` or `https://auth-v1.innate.bot` | Auth token issuer |
| `cmd_duration_sec` | float | 0.1 | How long each action command is published |
| `cmd_publish_hz` | float | 50.0 | Publish rate of `cmd_vel` during an action burst |
| `poll_period_sec` | float | 0.02 | Main-loop sleep between action-queue polls |
| `latency_report_sec` | float | 5.0 | Interval between RTT summary log messages |
| `image_send_hz` | float | 49.0 | Target rate for streaming images to the server |
| `consecutive_stops_to_complete` | int | 20 | Number of consecutive STOP actions before the goal succeeds |

---

## Connection lifecycle

1. A `NavigateInstruction` goal arrives.
2. The node creates an `UninavidWsClient` and calls `connect(instruction)`.
3. The client opens a WebSocket with a `Bearer` token (if `service_key` is
   configured).  On a **401** response the token is renewed and the
   connection is retried once.
4. The natural-language **instruction** is sent as the first text frame.
5. Two concurrent loops run on the connection:
   - **Send loop** — streams camera frames at `image_send_hz`.
   - **Receive loop** — parses action responses from the server.
6. The connection closes when:
   - The server sends `consecutive_stops_to_complete` consecutive `STOP`
     actions → goal **succeeds**.
   - The goal is **cancelled** or **preempted** by a new goal.
   - A connection error occurs → goal **aborts**.

---

## Robot → Server

### First message (text)

The raw instruction string, e.g. `"go to the red chair and stop"`.

### Subsequent messages (binary) — camera frames

Each message has the structure:

```
<JSON header>\n<raw image bytes>
```

#### JSON header fields

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `type` | string | `"image"` | Always `"image"` |
| `format` | string | `"jpeg"` | Encoding from ROS `CompressedImage.format` |
| `stamp_sec` | int | `1741152778` | ROS header stamp seconds |
| `stamp_nanosec` | int | `482000000` | ROS header stamp nanoseconds |

#### Example (pseudo-bytes)

```
{"type": "image", "format": "jpeg", "stamp_sec": 1741152778, "stamp_nanosec": 482000000}\n\xff\xd8\xff...
```

The first `\n` byte is the delimiter; everything after it is the raw
compressed image data.

---

## Server → Robot

Each message is a **text WebSocket frame** containing a **comma-separated
list of integers**:

```
<stamp_sec>,<stamp_nanosec>,<action>,<action>,...
```

- The first two integers echo the image stamp the server is responding to.
  This enables round-trip-time measurement and dropped-frame detection.
- The remaining integers are action codes (one or more).

### Action codes

| Code | Name | `linear.x` (m/s) | `angular.z` (rad/s) |
|------|------|------------------:|--------------------:|
| `0` | STOP | 0.0 | 0.0 |
| `1` | FORWARD | 0.3 | 0.0 |
| `2` | LEFT | 0.0 | +0.8 |
| `3` | RIGHT | 0.0 | −0.8 |

### Example exchange

```
server  →  "1741152778,482000000,1"    # forward
server  →  "1741152779,100000000,2"    # turn left
server  →  "1741152779,500000000,0"    # stop
```

Each action is executed as a `geometry_msgs/Twist` burst on `/cmd_vel`
(remapped to `/cmd_vel_scaled`) for `cmd_duration_sec` at `cmd_publish_hz`,
followed by a zero-velocity stop message.  Unknown codes are silently
ignored.
