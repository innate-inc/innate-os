# Verification log — 14 September 2026

## Hand yaw restored — 15 September 2026

- Personal control previously forced yaw to zero. It now uses fused heading
  from the thumb/index-base frame, with relative anchoring, a small dead zone,
  filtering and the existing 0.45-rad swivel bound. Pinch, scale and position do
  not enter its heading calculation. Synthetic rotations remain one-to-one at
  level, 90-degree downward pitch and beyond, including with correction enabled.
- Existing pure pitch/roll/grip and floor takes supply a tilt/base-shape correction.
  The correction cannot absorb camera-vertical rotation because its features
  are invariant to that rotation. Training uses the first two thirds of each
  take; the last third selects bounded coefficients (3.27-degree RMS residual,
  including a second inference pass over the same saved floor videos).
  Both repeats and yaw/lateral takes are excluded from fitting. These results
  are personal replay/geometry checks, not unseen-user tracking accuracy.
- Swiveled reach projection now checks bounds in the corresponding unswiveled
  workspace. A reachable requested pitch takes priority over a nearer posture
  with the wrong pitch, preventing the claw getting stuck after a floor turn.
- Regression tests cover both physical turn directions, floor grasp, interrupted
  yaw/reanchor, pitch return, pinch isolation, invalid calibration coefficients,
  control limits, tracking holds and camera cleanup.
- Final checks: 22 JS tests and 31 Python tests pass. Both floor replay sources
  preserve pitch, pinch/lift and sub-30-mm grasp-position error with yaw enabled.
  The personal browser replay also turns the physical base in both directions.
  Build and physics checks run in a clean worktree against current main; the
  robot asset route resolves the same URDF package as physics, including its
  rename from mars_sim to mars_description. Camera data/profiles remain local.

## Floor calibration applied — 15 September 2026

- All seven additional poses were comfortable: 409 accepted frames in session
  cf2f314a-75c0-42d6-939b-67c0e3687d25. The original controller read the 80-degree
  grasp as roughly 15–30 degrees of pitch plus unwanted roll. A depth error also
  made the visibly closed lifting pinch appear partly open.
- Added a bounded local refinement using 3D wrist orientation and palm-normalized
  image bone vectors/gap. Short projected bones stay short instead of amplifying
  their noisy direction. Smooth corrections separate tilt from apparent position
  and roll. Deep-grasp opening uses the visible thumb/index gap with taught curves.
- Fitting weights each pose equally. The original 15 training examples constrain
  their established response; six floor examples add the refinement. Both neutral
  repeats are excluded. On accepted frames, original rotation medians change by
  at most about one degree. The floor repeat returns within about four degrees.
  The user's resting distance changed substantially by that repeat, so absolute
  position is not scored there; live movement retains its relative reference.
- Recorded-landmark replay passes through the actual UI, websocket and physics:
  halfway and steep tilt, lowering, opening, closure, lift, neutral return and
  Stop, without hidden reanchoring. An independent rerun of MediaPipe on the saved
  videos produces fresh landmarks; those also pass the same physics replay.
  Only transitions between separate static takes are interpolated in the harness.
- Fresh-inference replay averages approximately 44 degrees halfway down,
  83 degrees steep/open, 76 degrees closed and 81 degrees lifting. Grip averages
  97% open, then effectively closed through the lift. Position errors during the
  floor sequence stay under 30 mm. These are calibration/replay results, not a
  general accuracy benchmark or proof of performance on unseen gestures.
- Existing recorded-pose replay still passes both pitches, both rolls, gripper
  open/closed, recentering, tracking hold/resume, Escape and camera cleanup.
  Nineteen JS tests, two profile-validation tests, the production build, Ruff
  and whitespace checks pass. Original videos and coefficients are preserved.
- Promoted the candidate with a timestamped backup of the prior active profile,
  restarted the dedicated server, and restored the level 70 mm ending pose through
  the normal simulated servos. The test server is stopped. The live studio shows
  the 23-pose calibration; webcam tracking is on and awaits a relaxed starting hand.

## Downward pitch and focused floor exercise — 15 September 2026

- The personal controller and server capped downward pitch at 0.65 rad (37
  degrees). Personal mode now accepts up to pi/2 downward; upward and legacy
  limits stay as before. Actual IK/joint limits, motion speed caps and the motion
  watchdog still govern the achieved pose. Existing learned gains are unchanged.
- Physical regression checks cover 80-degree approach, open/close at a 12 mm
  grasp-point height, lifting while closed, 90-degree pitch near the floor, and
  holding/reanchoring that tilt. Settled positions remain within 4 mm and pitch
  within 0.025 rad; Cartesian/joint speeds retain their caps.
- Added a separate seven-pose floor study with matching position/opening for the
  three tilt examples, a near-floor open/closed pair, lift and held-out repeat.
  Floor sessions use a separate localStorage key and study-data/floor directory.
  The original saved 16-pose catalogue hash was checked and remains identical.
- 17 JavaScript tests and 26 Python tests pass. The synthetic-camera browser
  exercise passes recording, pose/landmark storage, hash, refresh/resume, Escape,
  labels, all seven steps, completion cleanup and mobile layout. Original recorded
  landmark replay still passes both pitches/rolls, grip, recenter and tracking
  hold/resume. No tests access the live webcam. Build, Ruff and whitespace checks
  pass. New user floor recordings are needed before fitting the extended response.
- Activated the updated dedicated server on port 8840 and stopped the isolated
  test server. Opened the floor exercise with the webcam on and the first level
  pose settled. Whole-robot side framing was visually checked in the app; the
  exercise awaits the user's first recording.

## Studio display correction — 15 September 2026

- Live control was restoring the study's last close orbit angle, cropping the
  robot. It now opens and resets to a full-robot view that fits the canvas aspect
  ratio. Manually orbiting still works; a zero-size hidden canvas is ignored.
- The heading has its own space above the 3D canvas. Lower controls use ordinary
  page scrolling, with the simulator kept visible beside them on desktop.
- Production build and browser checks pass at 1280, 800, 390 and 320 pixels wide:
  no horizontal page overflow, heading overlap or page errors; settings and Reset
  remain reachable. The matching page still renders. No webcam or motion commands
  were used in these layout checks. The learned controls and recordings are intact.

## Personal profile from the completed study

- All 16 matches were marked comfortable: 938 frames with image and world
  landmarks. Fitting uses 15 equally weighted pose medians; the final neutral
  repeat is excluded. Recordings and coefficients remain local and gitignored.
- The old roll direction opposed the taught gesture. The fingertip frame also
  interpreted closure as roughly 35 degrees of pitch. The fitted thumb/index-base
  frame, direction-specific gains and aperture calibration separate those actions.
- Recorded rotation medians reproduce the taught ±29-degree pitch and ±40-degree
  roll; the reserved neutral repeat is about −2.8 degrees roll and +2.4 degrees
  pitch. These are calibration/consistency results, not an independent benchmark.
- Resting position changed during the recording session. Translational pair
  differences set the live movement transform; a comfortable live reference and
  explicit Recenter avoid treating the original camera coordinates as permanent.
  The neutral repeat would drift about 45 mm under a fixed original position
  reference, so no claim of absolute hand-position accuracy is made.
- Initially, overlapping yaw and sideways gestures led to disabling an additional
  hand-yaw offset. This was superseded by the direct-heading fix documented above.
- A real-video check exposed accumulated pitch after interruptions and an
  infeasible close-reach/tilt combination. Personal pitch now remains an absolute
  requested angle. The arm prefers the taught tilt by projecting reach to a nearby
  valid solution; ordinary joint limits remain the fallback. Projection occurs
  before the Cartesian speed cap, and does not bypass servo limits or physics.
- Recorded-landmark browser replay checks both pitch/roll directions, actual
  gripper opening/closure, recentering, tracking hold/resume, Escape and camera
  cleanup. Separate inference replay uses the actual saved videos and real
  MediaPipe, rather than injected landmarks. No test accesses the live webcam.
- Final verification passes: 17 JavaScript tests, 23 Python tests, both personal
  browser flows, production build, Ruff and whitespace checks. With real video
  inference, settled pitch averages are −0.471 and +0.447 rad (about −27/+26
  degrees), returning within 0.01 degrees of level after each tilt. No socket or
  page errors were reported. The recorded-landmark flow also passes both rolls,
  gripper closure/opening and tracking recovery. Desktop layout inspected.
- Activated the fitted profile in study-data/active-profile.json and restarted
  port 8840. The live studio shows the taught 130 mm neutral pose and 55% opening.
  Webcam enabled; the UI is awaiting a relaxed hand for its live starting reference.
  The isolated test server was stopped. The operator's subjective check remains.

## User-guided pose matching

- Added a separate 16-pose exercise covering neutral, both directions of pitch,
  roll, yaw, translation, reach and grip, plus a repeated neutral check. The old
  controller does not drive or score the user's chosen gestures in this mode.
- All target poses settle within 5 mm and 0.035 rad through actual physics,
  with the existing arm speed cap and 350 ms motion watchdog. Only the owning
  study session can select catalogue poses; normal hand-movement commands are
  rejected while matching.
- Twenty Python tests and twelve mapping tests pass. Study tests cover pose
  reachability, ownership, watchdog/lease separation, local save/resume/redo,
  input validation and preserving earlier takes.
- Browser integration passes the full 16-step flow with a synthetic camera:
  actual MediaRecorder clips, replay, timestamped landmarks and target metadata,
  file hashes, comfort labels, refresh/resume, Escape, completion cleanup and
  mobile layout. No live-control movement commands are sent.
- A separate real MediaPipe run on an existing hold recording saved 43 frames
  with all 21 world landmarks, a camera clip and the robot viewing angle.
  Test data stays in ignored artifacts/study-test-data, separate from user data.
- The real-tracker check exposed a review-time ownership timeout. Study ownership
  now tolerates ten seconds without input, while the motion watchdog still stops
  after 350 ms. The page can restore the same pose and preserve a reviewed take;
  Escape also cancels a pending camera start. Normal live ownership stays two seconds.
- Production build, Ruff and whitespace checks pass. Desktop layout inspected.
  This collects user preferences; no new hand-to-arm mapping is learned or applied
  until the operator supplies the matches.
- Restarted port 8840 and opened match.html in the existing studio tab. Its real
  webcam is enabled and the first robot pose is settled, awaiting the user's
  gesture (0 of 16 saved). The isolated test server was stopped.

## Pitch response correction

- Pitch now uses the direction from the thumb/index bases to the fingertip
  midpoint. Tilting only those fingers, with stationary wrist/MCPs, rotates the
  claw; a closed pinch retains its pointing direction.
- Removed the pitch lock in the lowest 60 mm. Real joint limits and floor
  contacts remain active; roll retains its ground fade.
- Removed automatic pitch recentering as position changes. The commanded angle
  follows hand-angle changes, retaining the achieved tilt on a hold. Excess tilt
  at input/joint limits is discarded so a small reversal responds immediately.
- Twelve mapping tests and seventeen Python tests pass. New regressions cover
  finger-only tilt, input-limit reversal, fixed pitch during translation, and
  actual pitch response at ground level. Floor penetration stays below the
  existing 0.5 mm solver tolerance in the tested ground poses.
- Browser integration now includes finger-only pitch at ground. Free-space
  pitch checks use reachable angles; the earlier ±0.14 rad sweep from its test
  posture reached the real lower joint limit, so it was narrowed to ±0.08 rad.
- Both browser flows pass on the corrected version, including real MediaPipe
  inference on the saved recordings. Production build, Ruff and whitespace
  checks pass. Port 8840 was restarted, the existing tab refreshed, and its
  webcam restored. Live subjective pitch accuracy still needs the operator's
  check; no new orientation dataset or accuracy benchmark was recorded.

## Initial thumb/index orientation — pitch behavior superseded above

- Ten mapping tests pass, including isolated roll/pitch/yaw, stable closed-pinch
  frames, translation/scale invariance, degenerate-frame holds, angle wrapping,
  bounded rotation and reanchoring.
- Fifteen Python tests pass. Measured roll and reachable pitch preserve grasp
  position within 5 mm; pinching preserves orientation. Yaw drives the real base
  joint along a shoulder-centered arc. Unreachable pitch clamps within the
  nearest continuous feasible interval instead of pulling the grasp point away.
  Interrupted rotations reanchor from the achieved wrist pose.
- Synthetic browser integration verifies all three rotations through WebSocket
  and real physics, alongside thumb/index closure, ground return and tracking
  recovery. Real MediaPipe tests with the saved videos verify left/reach motion,
  tracking holds, reconnect, camera cleanup, permission denial and responsive UI.
- The first recorded-video run paused unexpectedly after its screenshot while
  other simulations/tests were active; the isolated rerun passed. Connection and
  visibility events are now captured in test failure diagnostics. One synthetic
  run also missed its 2.5-second gripper-settling deadline; a fresh rerun passed
  the complete flow. Timing under system load remains a usability check, not a
  claimed live orientation-accuracy or contention benchmark.
- Production build, Ruff and diff whitespace checks pass. The existing studio
  was refreshed on port 8840 and its webcam restored for calibration.
- Orientation is relative to the starting hand pose. The five-joint arm couples
  yaw to position; pitch range depends on reach, and roll/pitch fade near ground.
  World landmarks are monocular estimates. No physical robot commands are sent.

## Ground reference — current height mapping

- Six mapping tests pass: bottom-of-image calibration, returning to zero,
  sensitivity-independent ground, and a fixed vertical reference after reanchoring.
- Ten Python tests pass. Startup is within 4 mm of z=0; all 3D corners settle
  within 5 mm with open jaws. Closed-jaw floor tests confirm contacts prevent
  penetration (under 0.5 mm solver tolerance). A jaw can touch before the virtual
  grasp point reaches zero; actual floor contact reads Ground.
- Synthetic browser integration passes startup at ground, raising above 140 mm,
  lowering below 5 mm, fixed ground line, thumb/index control and tracking recovery.
  Finger-independence checks run above the floor, where contact cannot displace it.
- Real MediaPipe browser tests pass with the recording shifted down for ground
  calibration, then restored for movement: left/reach, tracking recovery, recenter,
  reconnect, permission denial, and responsive layout. Original recordings unchanged.
- Production build and Ruff pass. The visual floor now matches physics at z=0.
- The repository switched branches during verification. Source was preserved in
  commit 11fc53cf0; the user's request returned the main checkout to people-v0.
  The temporary isolation checkout was removed.

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
benchmark recordings and physical robot were not modified. At that stage, depth
was estimated from a single camera and wrist orientation was not retargeted.
The scene still provides free-space control without a pick-and-place object task.

## Pinch-centered refinement — 2026-09-15

The original studio now has an optional pinch controller, preserving the existing
personal profile as a fallback. Calibration uses the completed 16-pose refinement
alongside the original and floor sessions; all four repeats are excluded from
fitting and hyperparameter selection.

Final local checks:

- `test/pinch-replay.mjs`: four reserved held-pose repeats, per-axis orientation
  RMS **11.49° → 4.83°** versus the previous personal mapper.
- `npm test`: **30 passed**, including 20 close/open cycles, partial reopening,
  fixed-midpoint closure, rigid edge-on turns, and reanchoring.
- Simulator suite: **33 tests passed** before the additional profile-validation
  case; the final focused six engine/profile tests also passed. Grasp-pad errors
  stay below **3 mm** during opening/closing at normal height and **7 mm** near
  the floor; settled error is below **4 mm**. All use measured MuJoCo geometry.
- Browser replay from a fresh simulator: floor approach **76.1°**, closed grasp
  **74.5°**, closed lift **74.9°**. The measured grasp point rose from **16.3 mm**
  to **57.2 mm** during the lift. The jaws closed fully. Returning to the identical
  reference restored the original target within **2 mm**. Tracking loss,
  recovery, pause, rendering, and the browser error checks passed.
- Build, Ruff, and formatting checks passed.

Reports, the previous profile/source snapshots, and browser screenshots are local
under `artifacts/pinch-refinement/` and `artifacts/pinch-browser/`. The browser test
interpolates transitions between held-pose recordings; it does not claim those
transitions were observed human motion. Four reserved poses are a limited check,
and subjective feel still requires live use. Full opening corresponds to actual
finger separation, so a recording labelled “open” is not forced to 100% opening.

## Direct fingertip rotation — 2026-09-15

Superseded by the Innate IK correction below. These checks used rotations in
the starting hand frame and missed the camera-axis twist reported by the user.

The operator requested one-to-one roll, pitch and yaw while keeping the existing
midpoint. Direct mode bypasses angular pose fitting and uses one rigid
alignment of the thumb–index frame. Position/grip coefficients are unchanged;
unit tests also compare their frame-by-frame outputs with the previous mapper.
The earlier **4.83°** held-pose result above describes the learned mapper, not
this direct mode or live tracking accuracy.

- **40 JavaScript tests passed.** Each axis has unit gain at ±20°, ±40°, and ±60°;
  mixed rotations, fingertip-only motion, contact/reopening, mirrored jaw order,
  vertical pitch, sensitivity independence, and reanchoring are covered.
- **36 Python engine/profile tests passed**, including interrupted-yaw
  reanchoring, grasp-point stability, watchdogs, transport ownership, and pause.
- Measured MuJoCo checks include ±60° yaw, 75° roll, combined rotations, and an
  80° downward grasp at a 12 mm target height with ±60° roll. Safe roll retains
  its full angle near the floor; a separate case checks clipping at the floor
  clearance boundary. Existing 1 rad/s setpoint limits remain enforced.
- Eleven synthetic browser poses passed through the actual UI, WebSocket, and
  physics. Maximum commanded rigid-rotation error was **0.000003°**; maximum
  settled joint-orientation error versus the reachable target was **0.064°**.
  Open-hand rotations preserve all three position commands. Closed-hand turns,
  reopening, tracking loss/recovery, pause, rendering, and browser errors passed.
- Yaw is the real base joint. The browser test explicitly accounts for heading
  caused by lateral translation; it does not claim an independent yaw wrist.
  Screenshots and full reports are in `artifacts/direct-rotation/`.
- Production build, Ruff, and JavaScript formatting passed. The original live
  profile and modified source files were copied to the report's `baseline/`
  directory before editing.

Synthetic geometry establishes controller gain, not monocular estimation
accuracy. Two coincident tips do not define a rotation frame; palm transport
preserves the last reliable alignment until the tips separate again.

## Innate IK and camera-axis correction — 2026-09-15

Reproduction with raw camera-space geometry: a **40° screen twist** became
**39.99° yaw** in the preceding controller. Its artificial shoulder swivel
then displaced the target sideways by **169.7 mm**. The corrected mapper
produces **40° roll**, with pitch/yaw effectively zero. See
`artifacts/innate-ik/twist-regression.json`.

The live path now calls `mars_arm.kinematics.ArmKinematics`, shared directly
with the Innate ROS IK node. It sends independent position and orientation
targets and compensates the grasp pad offset using KDL forward kinematics.
Unreachable orientation is limited at the requested point; it does not move
the point around the shoulder. Twisting a closed pinch no longer follows the
palm's incidental translation.

The actual IK runs in an isolated worker, with one asynchronous request in
flight. Camera tracking, physics, and control ownership continue independently.
Pausing invalidates old results. Native PyKDL is used when available; the Mac
uses the existing Innate image in a dedicated container without networking or
actuator access. No physical robot or running ROS stack is controlled by tests.

Regression tests use fixed camera-axis motion instead of deriving input axes
from the implementation's hand frame. Engine checks cover roll without lateral
sweep, fixed-point unreachable yaw, downward grasps, JSON state serialization,
late solver results, worker failure, and rejecting stale browser code. Browser
checks use the real app, WebSocket, and KDL-driven MuJoCo physics with synthetic
landmarks. Live hand-estimation accuracy and subjective feel remain unmeasured.

Final verification: 40 JavaScript tests, the 40-case Python regression run,
the final 10-case KDL engine suite, and the actual ROS-node smoke test passed.
All 12 browser poses used `innate_kdl`; maximum settled grasp-point error was
**0.287 mm**, and maximum orientation error against the reachable target was
**0.403°**. Impossible yaw was explicitly limited with the point held, rather
than counted as an achieved yaw rotation. The separate floor cases allow
sub-degree KDL/servo tilt error and require less than 3 mm grasp-point error.
Build, Ruff, formatting, tracking recovery, pause, and browser rendering passed.

### Roll alignment regression — 2026-09-16

Calibration now projects the observed thumb–index line into the actual gripper's
closing plane and chooses the nearest reachable roll. This removes a quarter-turn
starting offset while preserving the existing pitch, yaw and fingertip midpoint.
Calibration waits for a visible, nondegenerate line. A pause or tracking recovery
anchors to the held wrist without repeating the calibration correction.

Validation on the PR checkout: 44 JavaScript tests and the production build pass;
35 legacy physics/profile/study cases pass, as do all 11 direct KDL/MuJoCo cases.
The new physics case checks the actual finger-pad line at both ±90° roll endpoints
and holds the grasp point within 2 mm. The isolated ROS node smoke test verifies
its five-joint output, FK and next-solve seed. Repository pre-commit checks pass.
The browser regression also exercises closed-finger calibration, quarter-turn
recentring and pause/resume through the real studio UI and simulator.

These are deterministic controller and physics checks using synthetic landmarks,
not a measurement of live webcam pose accuracy or subjective comfort.
