# Hand control studio

Control a dedicated simulated MARS arm with a webcam. The browser renders the
repository's MARS URDF and measured joints from `VirtualMars`, the same MuJoCo
core used by the main simulator. This workspace needs no ROS or Docker and
cannot send commands to a physical robot.

From the repository root:

```sh
./innate-sim assets  # once, if simulator assets are not already installed
./sim/hand_control/run.sh
```

Open **http://127.0.0.1:8840/**. The launcher uses the sim Python environment,
installs it with `uv` if missing, builds the browser app, and starts the service.
Node.js 22.12+ and npm are needed for the build. Once built,
`sim/.venv/bin/python sim/hand_control/server.py` starts without rebuilding.

## Your fitted controls

When `study-data/active-profile.json` is present, the studio loads the locally
fitted profile and starts in the taught neutral arm pose, about 130 mm above the
floor. Enable the camera, hold your relaxed hand comfortably in view, then choose
**Start following**. **Recenter** establishes a fresh hand reference anywhere;
there is no ground-line calibration in this mode.

The profile learns roll/pitch directions and separate gains for each direction
from the pose medians. It uses the wrist and thumb/index bases for orientation,
so finger closure does not create the large false tilt of the fingertip frame.
Thumb/index aperture has taught closed/neutral/open values and compensation for
the apparent aperture change in the four rotation examples. Position uses the
three taught movement pairs with compensation for palm motion during rotation.
Turning the hand also commands yaw, using the full 3D thumb/index-base frame.
The heading remains observable at downward pitch. The MARS has no independent
wrist-yaw joint: yaw swivels the base, moving the claw in an arc about the shoulder.

Personal pitch commands specify an angle rather than accumulating angle changes.
Downward pitch can reach 90 degrees when the physical pose permits; upward pitch
keeps its existing 37-degree range. Original calibration coefficients are retained.
At an infeasible combination of reach and tilt, the solver first looks for a
nearby reachable radial position (at most 60 mm, inside the workspace). Position
and servo speed limits still apply. If none exists, ordinary joint-limit handling
applies. The UI reports reaching a limit.

With a floor refinement, smooth local corrections combine the original 3D wrist
frame with image-space palm/index bone vectors. Their lengths are normalized by
palm size, without amplifying a foreshortened bone into an unstable unit direction.
The corrections adjust pitch, unwanted roll and apparent translation during a
tilt. Original examples constrain the refinement near the established gestures.
For a downward grasp, the visible thumb/index gap takes over from the unreliable
landmark depth gap, using opening curves learned from the floor poses.

Fit or inspect a completed study with:

```sh
cd sim/hand_control
node calibrate.mjs study-data/SESSION_UUID
```

The script requires the 16 accepted comfortable matches, fits 15 pose medians,
and excludes the final neutral repeat from fitting. It saves a calibration report
beside the recordings and keeps earlier profile versions when overwriting. The
profile is loaded at server startup; restart the dedicated server to apply it.
`--profile PATH` selects another profile; a missing path selects the default
controller. Both modes retain tracking holds, session ownership and Stop.

To remove unwanted heading changes during the existing pitch/roll/floor poses,
without collecting more examples:

```sh
node calibrate_yaw.mjs study-data/active-profile.json
```

This writes a candidate under `artifacts/yaw-mapping/`, preserving the live
profile. Replay it with `server.py --port 8841 --profile
artifacts/yaw-mapping/candidate-profile.json` before promoting it. The correction
uses tilt and thumb/index-base triangle shape, which are invariant to a turn
about camera vertical; real yaw
therefore retains a one-to-one angle response. Calibration does not use hand
position, size, or fingertip gap. Both repeated poses and the original yaw/sideways
takes are withheld. Candidate selection uses the last third of each remaining
take, after fitting its first two thirds. This is within-session validation.
An optional third argument accepts the floor-video inference JSON produced by
`test/floor-inference.mjs`, so repeated tracker output from the same saved videos
can be included. No new user recordings are required.

These recordings personalize controls, rather than establish general model
accuracy. Resting hand placement drifted during the study; translation is relative
to the current live reference. The repeat checks rotation consistency, and is not
a claim of accurate absolute camera-to-robot positioning.

## Teach your hand — pose matching

Open **http://127.0.0.1:8840/match.html** or choose **Teach it what feels natural**.
This mode collects the user's intended hand-to-robot mapping without applying or
scoring against the existing controller. No ground calibration is required.

The robot holds 16 physically reachable poses: neutral, pitch, roll, yaw,
sideways, height, reach, opening/closing, and a repeated neutral pose. Enable the
camera, make whatever gesture feels natural for the shown pose, and press Space
or **Record my gesture**. A two-second countdown precedes a two-second take.
Review it, then keep it as natural or awkward, or record again. Poses can also
be marked impossible to match intuitively. Escape pauses; Previous revisits a
pose. Completion and reload resume from server-confirmed saves.

Only accepted takes are stored, under ignored `study-data/SESSION_UUID/`:

- `manifest.json`: full pose catalogue, version/hash, and latest accepted take
  for each pose. Repeats remain separate; neutral repeat is for consistency checks.
- Per-take JSON: intended target joints/position/orientation, measured robot state,
  time-stamped image and world hand landmarks, tracking metadata, camera dimensions,
  robot viewing angle, comfort label and optional notes.
- The corresponding local WebM/MP4 clip and its SHA-256. Redo retains earlier
  takes on disk, while the manifest points to the latest preference.

The browser retains the session ID for resume. A new session preserves previous
recordings. Test servers should use `--study-dir artifacts/study-test-data`.
These are preference-calibration records, not a hand-detector accuracy or latency
benchmark. This mode does not fit or apply a model automatically: after recording,
review the clips/labels, compare candidate hand features and mappings, and check
the repeated pose separately before changing control behavior.

### Additional floor-grasp poses

Use **Fine-tune floor grasps** or `/match.html?focus=floor` for seven additional
matches: level, 45-degree and 80-degree tilt at the same location, open/closed
near the floor, lift while closed, and a repeated level pose. The side view makes
downward pitch easier to see. The low grasp point is 12 mm above the floor, so the
exercise can settle with either jaw opening instead of pushing into the ground.

This exercise uses its own browser session and `study-data/floor/SESSION_UUID/`
directory. It preserves the original 16-pose catalogue and calibration. The final
repeat is reserved for checking consistency. Completing the exercise saves data;
the additional fit is reviewed and applied separately, after the user is done.

Create a candidate with:

```sh
node calibrate_floor.mjs study-data/active-profile.json study-data/floor/SESSION_UUID
```

The default output is `artifacts/floor-mapping/candidate-profile.json`, with a
report alongside it. Both neutral repeats are excluded. Pose weights are equal;
the original coefficients remain in the profile. Replay the candidate on an
isolated server before promoting it to the active profile. The two-second takes
do not establish absolute hand position: use a comfortable live reference and
Recenter after changing seating position or distance from the camera.

## Default controls (without a fitted profile)

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
5. Turn and tilt your thumb–index pose to rotate the claw. Screen-plane twist
   controls roll, tipping toward/away from the camera controls pitch, and turning
   sideways controls yaw. Your hand pose when following starts is neutral.
   Pitch also works near the floor. Roll and pitch use the wrist; yaw
   swivels the whole arm, moving the claw along an arc about the shoulder.

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
- In live-control mode, video stays in the browser and is not recorded. Only bounded position,
  roll/pitch/yaw and gripper values, frame time, and session metadata go to the
  loopback simulator.
- The palm center uses the wrist and four MCP joints, so curling fingers does
  not directly move the position target. Palm scale combines wrist–middle-MCP
  and index–pinky-MCP lengths, including relative landmark depth to reduce
  foreshortening. Its log change estimates reach. This remains a monocular
  depth proxy, not a metric hand position.
- Thumb/index fingertip distance normalized by palm size controls jaw opening.
  Middle, ring and pinky fingertip positions do not affect the jaws. A gap below
  0.18 palm units closes them; 1.13 or above fully opens them. If either control
  fingertip is clipped, the gripper holds while the visible palm can move the arm.
  A dashed line in the camera overlay highlights the measured gap.
- Rotation uses MediaPipe's estimated 3D world landmarks, with image/depth
  landmarks as a fallback. The thumb–index gap defines the jaw axis; the direction
  from their bases to their fingertip midpoint aims the claw, so tilting the
  fingers works even with a stationary wrist. The bases stabilize a closed pinch.
  Clipped control fingers hold orientation. Relative rotation has a
  0.025 rad dead zone and 100 ms smoothing. Missing/degenerate orientation holds
  the previous rotation. Long tracking losses reanchor orientation as well as
  position. This is monocular pose estimation, not a measured 6D hand pose.
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
  motion freshness. Live-control ownership expires after two seconds without
  client input; pose matching allows ten seconds to preserve review continuity.
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

The five-joint arm cannot independently match all six pose coordinates. Roll
uses joint5; pitch uses exact planar IK derived from the URDF and stays within
the nearest continuous range of reachable pitches. Pitch tracks changes in the
hand angle from the achieved claw pose. Translation preserves that tilt whenever
reachable; the neutral no longer moves to the middle of the available range.
Clamping discards excess tilt so reversing away from a limit responds immediately.
Yaw rotates the Cartesian target around the shoulder;
joint1 follows the resulting angle. Hand offsets are bounded to ±1.2 rad roll,
±0.65 rad pitch and ±0.45 rad yaw. Roll fades to zero in the lowest 60 mm;
pitch remains active at ground level. The UI reports joint limits. Near the
floor, real jaw contact can lift the grasp point slightly as the claw tilts.

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
../.venv/bin/python -m unittest test_engine test_pose_study test_personal_engine -v
npm run build
# Start the server separately, then:
npm run test:browser
node test/interaction.mjs
# Pose matching: use a separate server with --study-dir artifacts/study-test-data
STUDIO_URL=http://127.0.0.1:8841/ node test/study.mjs
# Personal profile server: use --profile PATH and the matching recorded session
STUDIO_URL=http://127.0.0.1:8841/ node test/personal-browser.mjs study-data/SESSION_UUID
STUDIO_URL=http://127.0.0.1:8841/ node test/personal-inference.mjs study-data/SESSION_UUID
```

The twelve mapping tests cover independent 3D directions, bounds, dead zones,
calibration, palm turns, clipping, thumb/index separation, independent rotations,
pinch-frame stability, finger-only pitch, angle wrapping, immediate reversal at
pitch limits, and reanchoring. Seventeen Python tests
cover all eight 3D workspace corners (<5 mm measured settled error), arm speed,
real gripper opening/closure/hold, watchdogs, token ownership, stale-frame
recovery, and a slow state sender keeping only the latest snapshot. Ground checks
cover startup at zero and floor contact across reach and gripper configurations.
Rotation checks cover measured roll/pitch with a fixed grasp point, real base yaw,
unreachable pitch, rotation bounds, pinching without changing wrist pose, and
resuming from the achieved orientation after an interrupted turn.
Pitch regressions verify a fixed tilt during translation and a real response
at ground height, with floor contacts still active.

`test/browser.mjs` uses the saved calibration videos under
`benchmarks/hand_tracking/data/` as a test-only canvas camera in headless Chrome.
The test translates the input video vertically for bottom-of-frame calibration;
the saved footage is unchanged. It runs real MediaPipe inference and checks
measured left/forward movement,
tracking loss/recovery, Esc, recentering, connection recovery, permission denial,
camera cleanup, and responsive layout. `test/interaction.mjs` separately injects
synthetic landmarks to check thumb/index and orientation-to-physics integration, classification
flicker, tilted palms, brief/long tracking loss, and a 480 ms inference delay.
These synthetic tests verify the controller, not real finger-gap model accuracy.

Neither test accesses the webcam. `CHROME_PATH` selects Chrome and `STUDIO_URL`
selects a separate test server. Screenshots stay under ignored `artifacts/`.
Restart the test server between browser scripts so each begins with a fresh
robot pose; use port 8841 to keep testing separate from the operator's studio.
The operator's live webcam and closed-hand gestures remain a usability check;
the saved dataset does not contain a dedicated thumb/index opening-and-closing recording.
The synthetic rotation checks do not establish live orientation accuracy.
