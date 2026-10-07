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
checks at time of use, and physically verified accuracy. That integration is
not included in this diagnostic.

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
