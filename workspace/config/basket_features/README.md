# Woven basket recognition and approximate pose

This stationary diagnostic recognizes MARS-47's 50 × 30 × 16 cm woven basket
from reference images. It returns visible-face pose and approximate exterior
rim geometry in camera coordinates. It does not command the robot or change
any existing approach, drop, tracking, or safety behavior.

## Run

Requires Python 3.10+, NumPy and OpenCV with SIFT. Live capture additionally uses
`websocket-client`. MARS-47 already has these dependencies. On another computer,
use a virtual environment and install the accompanying requirements file.
Keep the package directory layout when copying it.

```sh
python scripts/basket_pose_api.py --host 127.0.0.1 --port 9071
curl http://127.0.0.1:9071/health
curl --fail-with-body -H 'Content-Type: image/jpeg' \
  --data-binary @frame.jpg http://127.0.0.1:9071/detect
```

The API defaults to loopback. For remote hosting, place it behind the deployment's
normal authenticated reverse proxy. One request runs at a time; OpenCV worker
threads can be configured with `--threads`. No robot credentials or ROS runtime
are needed on the inference computer.

On MARS-47, capture fresh images and save JSON plus annotated previews:

```sh
python scripts/basket_pose_probe.py --live --frames 3 --output /tmp/basket-check
# Same camera path through an HTTP server, forwarding the original JPEG bytes:
python scripts/basket_pose_probe.py --live --frames 3 \
  --api-url http://127.0.0.1:9071 --output /tmp/basket-api-check
# Saved image:
python scripts/basket_pose_probe.py --image frame.jpg --output /tmp/basket-check
python -m unittest discover -s tests -p 'test_basket*.py' -v
```

## Inputs and outputs

`POST /detect` accepts an original, full-FOV JPEG or PNG, up to 8 MiB. Supported
sizes are the robot's 1280×720 native left-camera image and its nonuniformly
resized 640×480 stream. Cropped images cannot reuse this calibration.

Optional headers:

- `X-Calibration-Id`: match the SHA-256 returned by `/health`; mismatches return 409.
- `X-Captured-At-Unix`: frame time in epoch seconds. Frames over 3 seconds old or
  over 1 second in the future at request receipt return 422. Hosts need synced clocks.
- `X-Request-Id`: copied to the response (up to 128 characters).

Every image is detected independently. A missing target returns `detected: false`
without a previous pose. A recognition may return `best.pose: null` if it does
not fit the metric face model. `pose_ambiguous` marks similar-fitting planar
orientations. Inlier counts, reference coverage, reprojection error and candidate
alternatives are evidence, not a calibrated probability of correctness.

Coordinates are optical-camera coordinates: **x right, y down, z forward**.

- `visible_rim_midpoint_camera_m`: midpoint of the matched face's outer top edge.
- `rim_forward_depth_m`: that midpoint's z component, not its straight-line range.
- `rim_range_m`: straight-line camera-to-midpoint range.
- `face_normal_yaw_camera_deg`: yaw of the matched face's inward normal relative
  to the camera. A short face differs from a long face by 90°. Opposite faces
  may look alike; this is not an absolute 360° basket heading.
- `outer_rim_corners_camera_m`, `nearest_outer_rim_point_camera_m` and
  `nearest_outer_rim_range_m`: the user-measured rectangular basket extruded
  behind the matched face. This is an **approximate exterior**, not the clear
  interior opening or a validated drop target.
- `basket_center_camera_m`: centre of the approximate rectangular volume.

Do not interpret camera-frame values as wheel clearance or arm coordinates.
A motion consumer would need the current camera-to-base transform, freshness
checks at time of use, and physically verified accuracy. The separate experimental motion skill below supplies the transform and
freshness checks; physical accuracy and a complete drop still need verification.

## How it works

Eight manually masked vertical-face references exclude the floor, people,
background lettering, top opening and contents. Full-resolution RootSIFT
features use exact descriptor matching in both directions. A 0.8 descriptor
ratio proposes correspondences; a homography must retain at least 12 unique
inliers, 45% of proposed matches and 12% of the reference face area. Planar PnP
plus nonlinear refinement uses live-calibration lens distortion. Metric output
requires at most 4 px RMS reprojection error. No image downsampling, learned
confidence score, temporal smoothing or stale-pose reuse is used.

The camera calibration is a snapshot of MARS-47's published left CameraInfo,
with independent x/y scaling for the native feed. The reference quadrilaterals
are manually annotated. Real wicker sides taper, curve and deform, so the
rigid rectangular model introduces metric bias even with a small reprojection
error. `metric_accuracy_verified` remains false.

## Validation scope

Tests cover metric geometry at several ranges/angles, clipping, nonuniform
resize, full rim geometry, invalid calibration/images, target loss, and the real
HTTP request/decoder/detector/response path including stale inputs and calibration
mismatches. Synthetic geometry checks prove coordinate math, not physical accuracy.

Live stationary checks and a user-provided approximate 25 cm / roughly-square
measurement are recorded separately with the session evidence. The initial
SIFT matcher failed the new angled view; RootSIFT and mutual matching recovered
it without adding that test image as a reference. Broad lighting, other similar
baskets, arbitrary views, and robot-motion performance remain unverified.

## Feature-guided sock-drop skill (experimental)

`innate-os/drop_in_box_features` is a separate alternative to
`innate-os/drop_in_box_aruco`. Start holding a sock with a clear approach path.
Like the ArUco routine, it lifts first, searches right for the basket, uses a
26 cm frontal waypoint when the face is more than 20 degrees oblique, then
performs final docking, release and retreat. It does not plan around obstacles.
The original ArUco skill is unchanged.

The skill uses full-resolution RootSIFT locally by default. To use the same
HTTP perception service on another computer, set `BASKET_POSE_API_URL` in the
skills-server process environment to its base URL, for example
`http://inference-host:9071`. Configure the environment before starting the
server; this change does not provision a remote service or restart ROS.
An API error is retried without silently switching backends.

The robot stops and settles before every image. Acquisition checks current
CameraInfo and requires a frame captured after the stop. Inference runs in a
cancellable wait; its worker has no hardware handles. Motion during inference
invalidates the result. API responses must identify the exact input image and
calibration. A fresh missing-basket result starts rightward rotation without
translation, followed by another stopped observation. Search stops after a full
odometry-measured revolution or the inherited phase timeout: 175 seconds for
initial acquisition/repositioning, 50 seconds for final docking/recovery.
The initial search is capped at 0.70 rad/s; close recovery at 0.40 rad/s.
Camera/API errors do not authorize blind search and retain three consecutive
stationary retries. If the basket is recognized but its pose fit is missing or
ambiguous, the robot waits for fresh observations until the existing phase
deadline rather than failing after three frames. A lower-ranked matched face
may supply the pose only if it passes the same ambiguity and fit checks. Final docking includes the odometry control time;
an individual observation has a 45-second limit. A failed head-settle check
stops before lifting. Existing grip, arm-pose, and clearance safeguards remain.

The visible face centre and inward normal are transformed with measured head
pitch into base_link. The base approaches a point 16 cm in front of that centre,
in steps of at most 15 cm at up to 0.10 m/s (the ArUco final-approach cap). Position tolerance is 4.5 cm and
normal-heading tolerance is 0.13 radians. After checking the held sock, it
reobserves before calculating release. If another move is needed, it checks
the held sock again after that move. Wrist release is 13 cm
inside the face and 8 cm to its right, preserving the rehearsed arm offset.
Thus docking is centred; release is deliberately offset inside the basket.
Arm motion, pitch selection, entry arc, release/shake, clearance, folding and
retreat inherit the ArUco implementation. Wrist heights are unchanged at
32 cm entry, 18 cm release, and 26 cm clearance. These inherited heights have
not been physically validated against this 16 cm basket. Existing IK selection and measured
pose checks precede opening; verified arm clearance precedes retreat.

Validation: hardware-free execution tests exercise success, lost/ambiguous
pose, grip loss, unreachable target, head timeout, cancellation, and failed
clearance. Regression tests require the arm routines and heights to match ArUco, and
exercise rightward search, full-revolution exhaustion, reacquisition, sensor
errors/cancellation, and the same frontal-waypoint geometry. Catalog registration and live stationary
camera capture were checked on MARS-47. **No physical approach, release, or
landing has been tested for this new skill.** Similar baskets, different
lighting, and the curved basket's metric bias remain unverified.

Run focused tests in the robot's ROS environment:

```sh
source /opt/ros/humble/setup.bash
source ros2_ws/install/setup.bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  tests/test_drop_in_box_features.py tests/test_basket_features.py \
  tests/test_basket_pose_api.py
```
