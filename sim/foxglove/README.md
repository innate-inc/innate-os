# Foxglove layout

`mars-default.json` is a ready-made Foxglove Studio layout for the sim:

- **3D** — TF tree rooted at `base_link`, `/scan` (lidar), `/mars/main_camera/points`
  (the main camera's depth point cloud)
- **Main camera** — `/mars/main_camera/left/image_raw/compressed`
- **Arm camera** — `/mars/arm/image_raw/compressed`
- **Raw Messages** — `/robot_info`, `/joint_states`

Import it after connecting to the bridge (see
[`../README.md#ros-access`](../README.md#ros-access)): **Layout → Import from
file** → `sim/foxglove/mars-default.json`.

Camera and point cloud topics are lazy — the sim only renders and publishes
them once a panel actually subscribes, so an Image or 3D panel can sit empty
for a second after you open it.

Not included by default (add manually if you need it):
`/mars/main_camera/depth/image_rect_raw` (raw 16-bit depth `Image` panel).

On a physical robot over Wi-Fi, repoint the Image/3D panels at the lighter
`/mars/main_camera/remote/*` topics instead — see the Foxglove section in the
[top-level README](../../README.md#foxglove).
