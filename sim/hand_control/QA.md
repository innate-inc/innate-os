# Verification log — 14 September 2026

## Thumb/index correction — current mapping

- User clarified the intended pair: only thumb/index fingertip gap now controls
  the gripper. Middle, ring and pinky fingertip movement does not affect it.
- Five mapping tests pass, including closed/open/intermediate aperture, ignored
  finger movement, scale normalization, and unchanged arm position on pinching.
- Updated synthetic-landmark browser integration passes through real physics:
  thumb/index together closes, apart opens, and index/middle movement has no
  effect. Tracking continuity and delayed-frame recovery checks still pass.
- Production build passes; instructions and overlay now show thumb/index. The
  existing studio tab was refreshed and its webcam restored.
- These are controller integration tests; no new live gesture accuracy benchmark.

## Index/middle gripper correction — superseded

- Gripper now uses only index/middle fingertip separation, normalized by palm
  size. Removed thumb aperture and whole-hand curl from the grip calculation.
- Five mapping tests pass, including full closure/opening, intermediate opening,
  unchanged grip with thumb/ring/pinky movement or clipping, depth normalization,
  and unchanged arm position when the two fingertips come together.
- Updated synthetic-landmark browser integration passes through the real physics:
  bringing index/middle together closes the jaws, spreading opens them, and a
  thumb/index pinch does not close them. Recovery checks continue to pass.
- Production build passes. UI instructions and the camera gap overlay use the
  requested pair. Existing studio tab refreshed and webcam restored.
- These controller tests use synthetic landmarks; live finger-gap detector
  accuracy is still an operator usability check.

## Earlier tracking and 3D control verification

Updated after the operator reported repeated interruptions and requested natural
finger-to-gripper control.

- Production build passes; model SHA-256 verified. Runtime model, WASM, URDF,
  meshes and UI remain local.
- Five mapping/calibration tests pass: all three independent position axes,
  bounds/dead zones, thumb/index pinch, fist closure, reopening, palm rotation,
  classification flicker, and clipped-fingertip grip hold.
- Eight Python tests pass: all eight 3D workspace corners with <5 mm measured
  settled end-effector error, 1 rad/s arm setpoint limit, actual joint6
  opening/closure/intermediate opening, grip hold, watchdog/session separation,
  token ownership/revocation, stale-frame recovery, and an 80 ms blocked sender
  retaining only the newest queued state without disconnecting.
- Real MediaPipe inference on the operator's saved videos passes calibration,
  measured left movement >45 mm, measured forward movement >20 mm on approach,
  hand-loss holds, reacquisition, Esc, recenter, actual socket reconnect requiring
  explicit restart, camera cleanup, permission-denial retry and mobile layout.
  One desktop test screenshot reports 29 tracking frames/s and 25 ms mean
  inference. This is a sample during the test, not a new controlled model benchmark.
- Separate synthetic-landmark browser integration passes pinch/fist/open control
  through WebSocket and real gripper physics while the arm stays within 3 mm;
  palm turns and handedness flips; brief tracking loss; a 480 ms inference delay;
  and returning at a new position after a 1.2-second absence. Recovery keeps one
  socket and one control session, with <4 mm position change after reanchoring.
  These tests verify controller behavior; the saved videos have no dedicated
  pinch/fist examples, so they do not establish live closed-hand detector accuracy.
- Desktop screenshot inspected, mobile has no horizontal overflow at 390 px;
  no browser page errors. MediaPipe emits its normal XNNPACK initialization log.
- Ruff passes and Python/JS/HTML/CSS formatting completed.
- Updated server restarted at 127.0.0.1:8840; the existing user tab refreshed and
  its camera restored. The live UI reaches calibration at 30 tracking frames/s,
  waiting for the operator's hand. No claim of live gesture accuracy yet.

The test instance ran separately on 8841. The main ROS/Docker simulator, recorder,
benchmark recordings and physical robot were not modified. Depth is estimated
from a single camera; wrist orientation is not retargeted. This version provides
free-space 3D position and gripper control, without a pick-and-place object task.
