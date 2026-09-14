# Hand control studio

Control a dedicated simulated MARS arm with a webcam. The browser renders the
repository's MARS URDF and measured joints from `VirtualMars`, the same MuJoCo
core used by the main simulator. This workspace needs no ROS or Docker and
cannot send commands to a physical robot.

From the repository root:

```sh
./sim/hand_control/run.sh
```

Open **http://127.0.0.1:8840/**. The launcher uses the sim Python environment,
installs it with `uv` if missing, builds the browser app, and starts the service.
Node.js 22.12+ and npm are needed for the build. Once built,
`sim/.venv/bin/python sim/hand_control/server.py` starts without rebuilding.

## Controls

1. **Enable camera**, then allow access.
2. Show one hand, lower its palm center to the **GROUND** line near the bottom
   of the camera picture, and hold still for about 0.8 seconds. This establishes
   the lowest usable hand position while leaving the palm and control fingers visible.
3. Choose **Start following**. The arm starts at ground level. Lift your hand
   to raise it; returning to the bottom line returns to ground. Move sideways
   for left/right. Approach the webcam to reach forward; pull back to retract.
4. Bring your thumb and index fingertips together to close the gripper.
   Spread them apart to open it. Only this gap controls the jaws. The gripper meter
   shows the measured jaw opening, not just the requested gesture.

**Space** toggles following, **Esc** pauses, and **R** recalibrates. The camera-off
button releases the camera. Switching tabs pauses and requires an explicit
restart. Adjust sensitivity under *Fine-tune the feel*. Drag to orbit the 3D
view; the round arrow restores the front view.

Brief tracking losses hold position and automatically continue when the hand
returns. After a longer absence or a large position change, hold briefly (about
0.24 seconds) to reanchor sideways/reach from the held robot pose. Height always
uses the calibrated ground line, including after a pause or loss of tracking.
Moving the returning hand to a different height smoothly moves the arm to that
height; it does not redefine ground.
Palm rotation and handedness classification flicker
no longer disconnect control. Keep one hand in view; this is a single-hand
tracker and does not authenticate hand identity.

The green marker shows the hand target; the robot and trail show measured
physics state. Tracking rate and inference time are measured in this browser.
They differ from the earlier native Python benchmark.

## Tracking and control

- Pinned MediaPipe Hand Landmarker **0.10.32**, full v1 model, runs in a bundled
  classic Web Worker. Its WASM loader requires `importScripts`. A single-hand
  tracker avoids repeatedly searching for a second hand during normal control.
  See [Google's browser guidance](https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker/web_js).
- All runtime assets are local. `prepare.mjs` reuses the experiment's model or
  downloads the same public file, verifies its SHA-256, and copies pinned WASM.
- Video stays in the browser, never uploaded or recorded. Only three bounded
  position values, a gripper value, frame time, and session metadata go to the
  loopback simulator.
- The palm center uses the wrist and four MCP joints, so curling fingers does
  not directly move the position target. Palm scale combines wrist–middle-MCP
  and index–pinky-MCP lengths, including relative landmark depth to reduce
  foreshortening. Its log change estimates reach. This remains a monocular
  depth proxy, not a metric hand position or a full wrist-orientation retargeter.
- Thumb/index fingertip distance normalized by palm size controls jaw opening.
  Middle, ring and pinky fingertip positions do not affect the jaws. A gap below
  0.18 palm units closes them; 1.13 or above fully opens them. If either control
  fingertip is clipped, the gripper holds while the visible palm can move the arm.
  A dashed line in the camera overlay highlights the measured gap.
- Ground calibration derives the lowest/highest usable palm center from the
  visible wrist, MCPs, thumb and index tips, with a 4% image-edge margin. It
  requires calibration near the bottom limit and freezes those limits afterward.
  A 4% image-height band makes zero easy to hold. Hand-size/depth changes can
  require recalibration; these are visibility margins, not a measured detector boundary.
- Vertical position maps that bottom line to zero and the top usable line to
  full height. Sensitivity scales lift from zero and never shifts the floor.
  Sideways and forward/back movement keep their relative anchors.
- Position and gripper use time-based smoothing (70 ms and 55 ms). Position has
  small dead zones. Calibration uses medians, a 0.8-second stable dwell, and at
  least eight samples. Continuity uses palm position/scale, not handedness labels.
- A **350 ms motion watchdog** holds the robot if fresh movement stops. Session
  keepalives preserve ownership while tracking is on hold; they never renew
  motion freshness. Ownership expires after two seconds without client input.
  A lost socket or explicit Stop revokes the session token.
- Commands require a session token, increasing sequence, finite bounded values,
  and a recent capture time. Stale frames hold without terminating the session;
  fresh frames can resume. Malformed commands stop control. The browser drops
  inference results older than 400 ms; the server rejects capture ages >450 ms.
- Each viewer has a one-slot snapshot queue and its own sending task. Slow
  clients receive the newest available state and cannot block the physics loop.
  An actual two-second send stall closes the socket cleanly. This replaces the
  former 25 ms timeout that silently stopped updates to an open connection.

## Physics

Damped least-squares IK uses MuJoCo's Jacobian, real joint limits and the
shoulder/head guard. The working box is x=[0.245, 0.335] m,
y=[−0.153, 0.047] m, z=[0, 0.27] m in robot coordinates. Cartesian targets
advance at up to 0.18 m/s and arm setpoints at up to 1 rad/s. The gripper uses
joint6 and its mirrored jaw, bounded by the model's actual mechanical limits,
with setpoint slew capped at 2.5 rad/s.

VirtualMars servos, damping, contacts and structural sag stay active. Gravity
and sag compensation support accurate holds. Stops remove the arm's
outstanding position target and freeze the gripper setpoint; physics inertia
and settling remain visible. Floor contacts can stop closed jaws before the
virtual grasp point reaches zero; the height readout shows Ground when a jaw
contacts the floor. The 3D floor and physics plane both use z=0.
This scene currently provides free-space arm and
gripper control, without a pick-and-place object task.

## Verification

```sh
cd sim/hand_control
npm test
../../sim/.venv/bin/python -m unittest test_engine -v
npm run build
# Start the server separately, then:
npm run test:browser
node test/interaction.mjs
```

The six mapping tests cover independent 3D directions, bounds, dead zones,
calibration, palm turns, clipping, and thumb/index separation and independence from other fingers. Ten Python tests
cover all eight 3D workspace corners (<5 mm measured settled error), arm speed,
real gripper opening/closure/hold, watchdogs, token ownership, stale-frame
recovery, and a slow state sender keeping only the latest snapshot. Ground checks
cover startup at zero and floor contact across reach and gripper configurations.

`test/browser.mjs` uses the saved calibration videos under
`benchmarks/hand_tracking/data/` as a test-only canvas camera in headless Chrome.
The test translates the input video vertically for bottom-of-frame calibration;
the saved footage is unchanged. It runs real MediaPipe inference and checks
measured left/forward movement,
tracking loss/recovery, Esc, recentering, connection recovery, permission denial,
camera cleanup, and responsive layout. `test/interaction.mjs` separately injects
synthetic landmarks to check thumb/index-to-physics integration, classification
flicker, tilted palms, brief/long tracking loss, and a 480 ms inference delay.
These synthetic tests verify the controller, not real finger-gap model accuracy.

Neither test accesses the webcam. `CHROME_PATH` selects Chrome and `STUDIO_URL`
selects a separate test server. Screenshots stay under ignored `artifacts/`.
The operator's live webcam and closed-hand gestures remain a usability check;
the saved dataset does not contain a dedicated thumb/index opening-and-closing recording.
