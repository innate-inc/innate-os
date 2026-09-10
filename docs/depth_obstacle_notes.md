# Depth obstacle avoidance — findings and open issues

Working notes for the branch that re-enables the camera obstacle layer. Written
for the PR description; everything here was measured on R7-27 unless stated.

## Why the depth layer was disabled

`costmap.yaml` carried a fully-configured SpatioTemporalVoxelLayer that was
deliberately left out of the `plugins` list:

> with a calibrated camera it marks phantom obstacles from floor leakage
> (measured 2x cross-track degradation + swerve spikes, R7-27 2026-07)

Measured cause: **the reconstructed floor sat 3.2° above horizontal**, rising
with distance. At STVL's 50 mm marking threshold that put **28% of the whole
floor** into the costmap as obstacles.

The depth itself was never the problem. Corridor plane-fit residual was
**0.9 mm RMS** — flat to under a millimetre. The cloud was simply pointed wrong.

## Two contributing errors

**1. Frame mismatch (fixed).** `pointcloud.cpp` back-projects disparity with
`P1`, so points land in the *rectified* left frame, but the cloud is stamped
`camera_optical_frame`, which `mars.urdf` defines as the *unrectified* left
camera. `R1` was applied to the images and never un-applied to the points.
`updateCloudRotation()` now composes `R1ᵀ` and applies it to both cloud
builders; `computeFootprintMaskCalib()` applies the inverse, since it projects
the other way.

**2. Mechanical head-mount error (corrected per-robot).** The remainder is
physical and does not come out with `R1`.

## The head-angle scale error — UNRESOLVED, wider than this branch

Sweeping the head produced a linear relationship, not a constant:

```
floor pitch = 3.665 + 0.0269 × head_angle_deg
```

| head | measured floor pitch |
|---|---|
| −20.04° | 3.126° |
| +0.09° | 3.667° |

So there are two superimposed errors: a **constant ~3.67° offset** and a
**2.69% scale error on the reported head angle**.

The Dynamixel tick conversion (`(pos−2048)·2π/4096`, `arm_control.cpp:36`) is
exact for a 4096-tick servo, so the scale term is most likely mechanical —
gravity sag in the head mount, growing with nose-down moment and roughly linear
over ±20°.

**This means TF's camera pose is wrong for every consumer, not just depth.**
That includes `brain_client/innate/geometry.py`, which reimplements the same
head kinematics for the VLM's spatial reasoning. A VLM reasoning about where
something is on the floor inherits the same error.

What this branch does instead: corrects it in the depth path only, via
`mount_pitch_correction_deg`, calibrated at the −20° navigation head position.
`mode_manager` holds the head there so the calibration stays valid.

Residual if the head does move, with one constant correction:

| calibrated at | worst error over −20…+20° |
|---|---|
| −20° (nav pose) | 1.08° → 18.8 mm at 1 m |
| 0° / midpoint | 0.54° → 9.4 mm at 1 m |

**The real fix is upstream**, and is deliberately out of scope here: either the
head servo's `homing_offset` (`arm_config.yaml:136` — one hardcoded constant,
≈30°, shared across every robot with no per-unit override or verification), or
a proper per-robot camera extrinsic calibration against `base_link`. Both move
where the head physically points, which is implicitly baked into VLM prompt
framing and manipulation poses, so neither belongs in a depth-obstacle change.

## Per-robot configuration — needs a proper home

`mount_pitch_correction_deg: -3.126` / `mount_roll_correction_deg: 0.398` are
**specific to R7-27**. They currently sit in
`mars_cam/config/stereo_depth_estimator.yaml`, which ships to every robot.

That is wrong and should not merge as-is. The existing per-robot pattern is
`data/*calibration_config/` (where `stereo_calib.yaml` lives); the mount
correction belongs there, read at startup. Left as-is for now only because it
unblocks on-robot testing.

## Why a corridor rather than the full field of view

The layer consumes `/mars/main_camera/points_nav` — base_link, already
height-filtered, x 0.25–1.0 m, |y| ≤ 0.22 m, z 0.02–0.36 m.

1. **Residual pitch error scales with range.** Capping at 1.0 m rather than
   2.5 m shrinks any leftover tilt's effect proportionally.
2. **It keeps the corridor off the lens periphery.** Held-out calibration
   validation measured epipolar error growing toward the frame edge on a ~98°
   FOV lens fitted with 5-parameter `plumb_bob`. At −20° head, 83% of corridor
   points land in the centre ring versus 43% at level.
3. **Every point outside the corridor can only ever mark a phantom obstacle** —
   the robot was never going to drive there.

Range budget: at `vx_max` 0.45 m/s with `ax_min` −0.3 m/s², stopping distance is
0.34 m; ~0.4 s of pipeline latency (8 Hz depth, 5 Hz costmap) adds ~0.18 m. A
1.0 m horizon leaves roughly 0.5 m of margin, and STVL's 2 s `voxel_decay`
accumulates a trail as the robot advances, so obstacles are remembered once they
fall below the corridor's near edge.

`z_min` 0.02 m sits just above the ~10 mm that can simply be rolled over.

## One height threshold, in one place

`nav_roi.z_min` in the depth node owns the height cut. STVL's
`min_obstacle_height` is set to 0.0 and its `max_obstacle_height` left wide, so
there are not two thresholds in different frames disagreeing with each other.

## Vibration — measured, not a factor

The spinning lidar and the driving base both visibly shake the camera. Measured
per-frame floor-fit spread:

| head | worst single-frame excursion | floor error at 1 m |
|---|---|---|
| −20° | 0.181° | 3.2 mm |
| 0° | 0.044° | 0.8 mm |

Well under any threshold in use, and far below the systematic tilt. Vibration is
4× worse at −20°, consistent with the servo holding against gravity, but still
does not drive any design decision here.

## Arm self-masking — two known gaps

`dynamic_footprint` projects the arm's collision boxes into the camera and the
depth node zeroes disparity under their convex hull before the filter chain, so
the arm never reaches the cloud. Two limitations remain, both documented rather
than fixed:

- **The hull is convex over all arm links together.** An arm in an L-pose fills
  its own concavity, which can erase a real obstacle sitting inside it. That is
  a false *negative* — worse than the phantom obstacles this branch fixes.
  Needs per-link hulls in `dynamic_footprint`.
- **A held payload is not masked at all.** Gripper contents are not in the URDF,
  so a carried object is marked as an obstacle.

## Also fixed along the way

- **Stereo sync tolerance 100 ms → 10 ms.** Both eyes are split from one
  2560×720 capture and share a timestamp; across 146 captures the observed skew
  had median and p95 of exactly 0.0 ms, with a single 68 ms mispairing. 100 ms
  allowed pairing frames two apart at 30 fps.
- **`Q` sign.** `stereoCalibrate` returns the left camera's origin in the right
  camera's frame, so a correctly-wired rig gives `T[0] < 0` and the calibrator's
  `if T[0] < 0: T = -T` branch fires on every good calibration. Rectification is
  unaffected (`stereoRectify` picks its axis from `sign(T)`), but the saved `Q`
  reprojects front-of-camera points to negative Z. Harmless today because every
  consumer takes an absolute value and nothing calls `reprojectImageTo3D` —
  documented, deliberately not changed.

## Verification

```bash
ros2 run mars_cam ground_plane_check     # floor pitch should now be near 0
ros2 topic hz /mars/main_camera/points_nav
```
