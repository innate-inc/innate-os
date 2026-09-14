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
