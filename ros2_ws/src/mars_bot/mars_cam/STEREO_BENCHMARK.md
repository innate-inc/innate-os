# Stereo Depth Benchmark Runbook (Final)

This runbook reflects the current `mars_cam` benchmark workflow after the latest updates.

It supports:

- one canonical recording reused across models
- runtime + Orin telemetry metrics (latency P50/P95/P99, FPS, drops, CPU/GPU/RAM/power/temp)
- lidar-vs-model overlay media (`L/M/D`) with non-overlapping labels and arrows
- quantitative charts from all valid projected points
- clean checkpoint-tagged comparison folders

## 1) Build and source

```bash
cd /home/jetson1/innate-os
innate build
source /opt/ros/humble/setup.zsh
source ros2_ws/install/setup.zsh
```

## 2) Benchmark in an isolated ROS graph

Do not replay bags while live camera/TF publishers are active.

```bash
innate service stop
ros2 node list | rg 'main_camera_driver|camera_container|stereo_depth_estimator|robot_state_publisher'
```

If those nodes are still listed, stop them before running benchmarks.
After benchmarking:

```bash
innate service start
```

## 3) Setup route (lightweight only)

Official Isaac ROS integration is intentionally out of scope for this workflow because of ROS/version compatibility drift on this stack.
Use only the lightweight MARS wrapper path.

### Lightweight Fast-FoundationStereo (supported path)

```bash
python3 /home/jetson1/innate-os/scripts/setup_stereo_routes.py \
  --route lightweight_fast_foundation \
  --install-python-deps
```

If `python3-venv` is missing:

```bash
sudo apt-get install -y python3.10-venv
```

This route installs:

- `ros2_ws/src/third_party/stereo_models/Fast-FoundationStereo`
- `.venvs/fast_foundation_stereo`
- `ros2 run mars_cam fast_foundation_stereo_node`

## 4) Prepare benchmark config

```bash
cp ros2_ws/src/mars_bot/mars_cam/config/stereo_depth_benchmark.example.yaml \
  /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

Edit `recordings/stereo_depth_benchmark.yaml`:

- set `canonical_bag`
- enable target models
- verify each model `launch_command`, `depth_topic`, `inference_topic`
- optionally set `disparity_topic`, `pointcloud_topic`, `costmap_topic`
- keep `telemetry.enable_tegrastats: true`

For Fast-Foundation, ensure `model_path:=.../model_best_bp2_serialize.pth` points to an existing checkpoint.

## 5) Record canonical bag once

```bash
CANONICAL_BAG=/home/jetson1/innate-os/recordings/stereo_canonical_$(date +%Y%m%d_%H%M%S)
ros2 run mars_cam stereo_depth_benchmark record-canonical \
  --output-bag "$CANONICAL_BAG" \
  --topic /mars/main_camera/left/image_raw \
  --topic /mars/main_camera/right/image_raw \
  --topic /mars/main_camera/left/camera_info \
  --topic /mars/main_camera/right/camera_info \
  --topic /tf \
  --topic /tf_static \
  --topic /scan \
  --topic /odom \
  --topic /imu
```

Stop with `Ctrl-C`, then verify:

```bash
ros2 bag info "$CANONICAL_BAG"
```

## 6) Run benchmark

```bash
ros2 run mars_cam stereo_depth_benchmark run \
  --config /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

Outputs under `output_dir` (default `recordings/stereo_benchmark_runs`):

- per-model run bags
- `stereo_depth_benchmark_*.json`
- `stereo_depth_benchmark_*.csv`
- `*_tegrastats.log`

Optional re-evaluation:

```bash
ros2 run mars_cam stereo_depth_benchmark evaluate \
  --config /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

## 7) Generate overlay media

### 7.1 Lidar-only overlay

```bash
ros2 run mars_cam stereo_depth_benchmark annotate \
  --bag <run_bag_dir> \
  --image-topic /mars/main_camera/left/image_raw \
  --lidar-topic /scan \
  --camera-info-topic /mars/main_camera/left/camera_info \
  --output-video <out_lidar.mp4> \
  --output-image <out_lidar.png> \
  --image-frame-index 150
```

### 7.2 Lidar-vs-model error overlay

```bash
ros2 run mars_cam stereo_depth_benchmark annotate \
  --bag <run_bag_dir> \
  --image-topic /mars/main_camera/left/image_raw \
  --lidar-topic /scan \
  --camera-info-topic /mars/main_camera/left/camera_info \
  --depth-topic <model_depth_topic> \
  --error-max-m 1.0 \
  --output-video <out_lidar_vs_model.mp4> \
  --output-image <out_lidar_vs_model.png> \
  --image-frame-index 150
```

Overlay semantics:

- `L`: lidar depth (m)
- `M`: model depth (m)
- `D`: absolute error `|M-L|` (m)

Rendering behavior:

- labels are placed to avoid overlap
- arrows connect labels to sampled points
- point color is based on error when `--depth-topic` is provided

Common depth topics:

- classical: `/mars/main_camera/depth/image_rect_raw`
- fast foundation: `/stereo/fast_foundation/depth`

## 8) Generate quantitative charts from all valid points

The chart flow uses all valid projected points, not only labeled overlay points.

```bash
ros2 run mars_cam stereo_depth_benchmark charts \
  --summary-json /home/jetson1/innate-os/recordings/stereo_benchmark_runs/stereo_depth_benchmark_<stamp>.json \
  --model classical_stereo \
  --model fast_foundation_23_36_37_i8 \
  --grid-rows 3 \
  --grid-cols 4
```

Chart outputs:

- `<summary>_distance_error_distribution.png`
- `<summary>_frame_region_error_heatmap.png`
- matching CSVs with counts + percentiles

## 9) Build a checkpoint-tagged comparison folder (existing bags)

Use this when inference bags already exist and you want a clean final deliverable package.

```bash
source /opt/ros/humble/setup.zsh
source /home/jetson1/innate-os/ros2_ws/install/setup.zsh

SUMMARY=/home/jetson1/innate-os/recordings/stereo_benchmark_runs/stereo_depth_benchmark_20260911_015139.json
FAST_MODEL=fast_foundation_23_36_37_i8
FAST_TAG=fast_ckpt_23_36_37_i8
OUT_DIR=/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_vs_${FAST_TAG}_$(date +%Y%m%d_%H%M%S)
export SUMMARY FAST_MODEL FAST_TAG OUT_DIR
mkdir -p "$OUT_DIR"
```

Create lidar reference + model overlays:

```bash
python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path

summary = Path(os.environ["SUMMARY"])
fast_model = os.environ["FAST_MODEL"]
fast_tag = os.environ["FAST_TAG"]
out_dir = Path(os.environ["OUT_DIR"])
rows = {row["model"]: row for row in json.loads(summary.read_text())}
c = rows["classical_stereo"]
f = rows[fast_model]

subprocess.run([
    "ros2", "run", "mars_cam", "stereo_depth_benchmark", "annotate",
    "--bag", c["run_bag"],
    "--image-topic", "/mars/main_camera/left/image_raw",
    "--lidar-topic", c.get("lidar_topic", "/scan"),
    "--camera-info-topic", c.get("camera_info_topic", "/mars/main_camera/left/camera_info"),
    "--output-video", str(out_dir / f"lidar_reference_for_{fast_tag}.mp4"),
    "--output-image", str(out_dir / f"lidar_reference_for_{fast_tag}.png"),
    "--image-frame-index", "150",
], check=True)

subprocess.run([
    "ros2", "run", "mars_cam", "stereo_depth_benchmark", "annotate",
    "--bag", c["run_bag"],
    "--image-topic", "/mars/main_camera/left/image_raw",
    "--lidar-topic", c.get("lidar_topic", "/scan"),
    "--camera-info-topic", c.get("camera_info_topic", "/mars/main_camera/left/camera_info"),
    "--depth-topic", c["depth_topic"],
    "--error-max-m", "1.0",
    "--output-video", str(out_dir / f"classical_stereo_lidar_vs_model_for_{fast_tag}.mp4"),
    "--output-image", str(out_dir / f"classical_stereo_lidar_vs_model_for_{fast_tag}.png"),
    "--image-frame-index", "150",
], check=True)

subprocess.run([
    "ros2", "run", "mars_cam", "stereo_depth_benchmark", "annotate",
    "--bag", f["run_bag"],
    "--image-topic", "/mars/main_camera/left/image_raw",
    "--lidar-topic", f.get("lidar_topic", "/scan"),
    "--camera-info-topic", f.get("camera_info_topic", "/mars/main_camera/left/camera_info"),
    "--depth-topic", f["depth_topic"],
    "--error-max-m", "1.0",
    "--output-video", str(out_dir / f"{fast_tag}_lidar_vs_model.mp4"),
    "--output-image", str(out_dir / f"{fast_tag}_lidar_vs_model.png"),
    "--image-frame-index", "150",
], check=True)
PY
```

Create comparison charts:

```bash
ros2 run mars_cam stereo_depth_benchmark charts \
  --summary-json "$SUMMARY" \
  --output-dir "$OUT_DIR" \
  --model classical_stereo \
  --model "$FAST_MODEL" \
  --grid-rows 3 \
  --grid-cols 4
```

## 10) Decision priorities

For navigation readiness, rank by:

1. `inference_latency_p95_ms`
2. `inference_latency_p99_ms`
3. `camera_to_costmap_latency_p95_ms` (if available)
4. `dropped_frames_pct`
5. `thermal_throttling_detected`
6. depth quality (`rmse_m`, `mae_m`, `% within 10cm`)

P95/P99 behavior matters more than average FPS.

## 11) Troubleshooting

- live graph conflict: `innate service stop` before `run`
- many `NaN` depth metrics: bad/missing `depth_topic` or no valid depth values
- missing costmap latencies: costmap topic was not published during run
- overlay has no labels: verify `--lidar-topic`, `--camera-info-topic`, `--depth-topic`
- duplicate downloads: `ps -eo pid,cmd | rg -i 'wget|curl|aria2c|pip install|apt-get'` then `kill <pid>`
- full Isaac ROS stack is intentionally not part of this runbook on this platform; use the lightweight wrapper path

## 12) Example final outputs generated

Generated comparison folders from this workflow:

- `recordings/stereo_benchmark_runs/classical_vs_fast_ckpt_20_30_48_i8_20260911_024333`
- `recordings/stereo_benchmark_runs/classical_vs_fast_ckpt_23_36_37_i8_20260911_024954`

Each contains:

- lidar reference MP4/PNG
- classical lidar-vs-model MP4/PNG
- fast-checkpoint lidar-vs-model MP4/PNG
- comparison charts (`distance_error_distribution`, `frame_region_error_heatmap`) + CSVs
- summary JSON/CSV
