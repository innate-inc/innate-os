# Stereo Benchmark Experiment Runbook

This runbook is the full end-to-end process to benchmark stereo models on one canonical recording, collect depth accuracy, and measure Orin Nano runtime behavior with emphasis on P95 latency.

## 0) Build and source

From repo root:

```bash
cd /home/jetson1/innate-os
innate build
source ros2_ws/install/setup.zsh
```

### 0.1) Run benchmark in isolation (important)

Do not benchmark while the full robot service stack is publishing camera/TF topics.
If live publishers and replay publishers overlap, TF warnings (`TF_OLD_DATA`) and
unstable latency/drop metrics are expected.

Stop services before benchmark replay:

```bash
innate service stop
```

After stopping, verify there is no live camera stack:

```bash
ros2 node list | rg 'main_camera_driver|camera_container|stereo_depth_estimator|robot_state_publisher'
```

If this prints any of those live nodes, stop the remaining process before continuing.

Restart services after benchmarking:

```bash
innate service start
```

## 1) Prepare benchmark config

Copy and edit the example:

```bash
cp \
  ros2_ws/src/mars_bot/mars_cam/config/stereo_depth_benchmark.example.yaml \
  /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

Choose one setup route before benchmarking:

### Route A: Official Isaac ROS Integration (FoundationStereo + ESS)

Use this when you want vendor-maintained ROS integration and can satisfy Isaac dependencies.

```bash
cp \
  /home/jetson1/innate-os/ros2_ws/src/mars_bot/mars_cam/config/stereo_model_sources.example.yaml \
  /home/jetson1/innate-os/recordings/stereo_model_sources.yaml
sudo -v
python3 /home/jetson1/innate-os/scripts/setup_stereo_routes.py \
  --route official_isaac_ros \
  --manifest /home/jetson1/innate-os/recordings/stereo_model_sources.yaml \
  --accept-eula
```

### Route B: Lightweight Fast-FoundationStereo (Humble-friendly)

Use this when you want to avoid Isaac ROS distro/toolchain jumps and own a thin local wrapper.

```bash
python3 /home/jetson1/innate-os/scripts/setup_stereo_routes.py \
  --route lightweight_fast_foundation
```

To install the model Python runtime as part of Route B:

```bash
python3 /home/jetson1/innate-os/scripts/setup_stereo_routes.py \
  --route lightweight_fast_foundation \
  --install-python-deps
```

Route B sets up:
- checkout: `ros2_ws/src/third_party/stereo_models/Fast-FoundationStereo`
- venv: `.venvs/fast_foundation_stereo`

If your system is missing `python3-venv`, run:

```bash
sudo apt-get install -y python3.10-venv
```

or skip venv creation:

```bash
python3 /home/jetson1/innate-os/scripts/setup_stereo_routes.py \
  --route lightweight_fast_foundation \
  --skip-venv
```

Edit `/home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml` and set:

- `canonical_bag` to your recorded canonical bag path
- each model's `launch_command`
- each model's `depth_topic`
- each model's `inference_topic` (topic used for inference latency/FPS/drop stats)
- optional `disparity_topic`, `pointcloud_topic`, `costmap_topic` for stage latencies
- `defaults.lidar_topic` and `defaults.camera_info_topic`
- `telemetry.enable_tegrastats: true` for Orin telemetry

If you want camera-to-costmap latency, each candidate's `launch_command` must bring up the costmap publisher path too. If not present, those metrics are `NaN`.

## 2) Record one canonical dataset

Record once and reuse for all models.

```bash
ros2 run mars_cam stereo_depth_benchmark record-canonical \
  --output-bag /home/jetson1/innate-os/recordings/stereo_canonical_$(date +%Y%m%d_%H%M%S) \
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

Use `/lidar` instead of `/scan` if that is your active topic.

Alternative launch helper:

```bash
ros2 launch mars_cam stereo_depth_benchmark_record.launch.py lidar_topic:=/scan
```

Stop recording with `Ctrl-C`. Copy that bag path into `canonical_bag` in your config.

## 3) Validate model launch wrappers (one-time smoke test)

The package includes:

- `classical_stereo.launch.py`
- `fast_foundation_stereo.launch.py`
- `light_stereo.launch.py`
- `igev_stereo.launch.py`
- `ess_stereo.launch.py`

Example smoke test:

```bash
ros2 launch mars_cam fast_foundation_stereo.launch.py \
  model_package:=your_pkg \
  model_executable:=your_exec \
  model_left_image_topic:=left/image_raw \
  model_right_image_topic:=right/image_raw \
  model_depth_topic:=depth/image_rect_raw \
  depth_topic:=/stereo/fast_foundation/depth
```

If your model node uses different internal topic names, override `model_*_topic` args.

## 4) Run the full benchmark experiment

```bash
ros2 run mars_cam stereo_depth_benchmark run \
  --config /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

The runner now checks for live camera/TF nodes and fails fast if they are still active.
Override only when you intentionally want a mixed live+replay graph:

```bash
ros2 run mars_cam stereo_depth_benchmark run \
  --config /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml \
  --allow-live-graph
```

For each enabled model, the tool:

1. starts the model launch command
2. starts `ros2 bag record` for reference + model outputs
3. replays canonical bag once
4. captures `tegrastats` during replay
5. computes depth-vs-lidar metrics
6. computes runtime latencies/FPS/drop metrics
7. writes summary rows

## 5) (Optional) Re-evaluate existing model run bags

If `run_bag` is set per model in config:

```bash
ros2 run mars_cam stereo_depth_benchmark evaluate \
  --config /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

## 6) Create lidar-tagged outputs for qualitative review

Tagged video:

```bash
ros2 run mars_cam stereo_depth_benchmark annotate \
  --bag /home/jetson1/innate-os/recordings/stereo_benchmark_runs/<model_run_bag> \
  --image-topic /mars/main_camera/left/image_raw \
  --lidar-topic /scan \
  --camera-info-topic /mars/main_camera/left/camera_info \
  --output-video /home/jetson1/innate-os/recordings/stereo_benchmark_runs/<model>_lidar_tagged.mp4
```

Tagged still frame:

```bash
ros2 run mars_cam stereo_depth_benchmark annotate \
  --bag /home/jetson1/innate-os/recordings/stereo_benchmark_runs/<model_run_bag> \
  --image-topic /mars/main_camera/left/image_raw \
  --lidar-topic /scan \
  --camera-info-topic /mars/main_camera/left/camera_info \
  --output-image /home/jetson1/innate-os/recordings/stereo_benchmark_runs/<model>_lidar_tagged.png \
  --image-frame-index 150
```

## 7) Output files and where to look

Under `output_dir`:

- per-model run bag directories
- `stereo_depth_benchmark_*.json`
- `stereo_depth_benchmark_*.csv`
- per-run `*_tegrastats.log`
- optional tagged MP4/PNG from `annotate`

## 8) Primary decision metrics (recommended)

For navigation model selection, rank primarily by:

1. `inference_latency_p95_ms` (lower is better)
2. `camera_to_costmap_latency_p95_ms` (if measured)
3. `dropped_frames_pct`
4. `thermal_throttling_detected` (must be false under expected duty)
5. depth quality metrics (`rmse_m`, `mae_m`, `% within 10 cm`)

Use median latency only as supporting context; keep P95/P99 as primary stability signals.

## 9) Quick troubleshooting

- `tegrastats_available=false`: `tegrastats` not found or failed to start.
- many `NaN` latency fields: referenced topic missing from run bag.
- benchmark fails with "isolated ROS graph" error: live camera/TF nodes are still running; run `innate service stop`.
- costmap latency `NaN`: costmap topic not produced during candidate run.
- high dropped frames: check model queueing, synchronization settings, and GPU saturation.
- latency appears in hundreds of seconds: update to latest `mars_cam`; older benchmark code mixed bag receive time with header epochs.
- high P95/P99 with good median: inspect bursty load in `*_tegrastats.log` (power/temp/GPU spikes).
- frequent Zenoh `unknownexpr_id` errors during replay: usually appears when replay/live publishers overlap on the same topics; use `classical_stereo.launch.py` for baseline replay so only the depth estimator runs.
- installer exits with `sudo access is required`: run `sudo -v` in the same terminal, then re-run.
- installer exits with EULA warning: re-run with `--accept-eula` (or set `ISAAC_ROS_ACCEPT_EULA=1`).
- `magic_enum::magic_enum` CMake target missing: rosdep dependencies were not installed; re-run without `--skip-rosdep`.
- `libcusolver.so.12` missing during ESS engine generation: CUDA 13 runtime is not available on host; re-run full rosdep install and ensure CUDA packages install successfully.
- `ros-humble-negotiated` apt failure on Route A: this usually means Isaac release and host distro are misaligned. Prefer Route B on Humble/Jammy.

## 10) Step-by-step from current state

This is the shortest path to finish one full benchmark cycle: record, run candidates, tag lidar overlays, then rank by stability/performance.

### 10.1 Re-source and verify benchmark CLI

```bash
cd /home/jetson1/innate-os
source /opt/ros/humble/setup.zsh
source ros2_ws/install/setup.zsh
ros2 run mars_cam stereo_depth_benchmark --help
```

### 10.2 Record canonical bag

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

Stop with `Ctrl-C` when done.

Verify required topics were captured:

```bash
ros2 bag info "$CANONICAL_BAG"
```

Make sure `Topic information` includes `/scan`, `/odom`, and `/imu` before continuing.

### 10.3 Point benchmark config at the canonical bag and enable models

```bash
cp ros2_ws/src/mars_bot/mars_cam/config/stereo_depth_benchmark.example.yaml \
  /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

Edit `/home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml`:

- set `canonical_bag: $CANONICAL_BAG` (paste resolved path)
- set `enabled: true` for models you want to compare now (for example `classical_stereo`, `fast_foundation_stereo`, `ess`)
- fill each enabled model's `launch_command`, `depth_topic`, and `inference_topic`
- keep `telemetry.enable_tegrastats: true`

### 10.4 Run benchmark + analytics generation

```bash
ros2 run mars_cam stereo_depth_benchmark run \
  --config /home/jetson1/innate-os/recordings/stereo_depth_benchmark.yaml
```

This writes per-model run bags and summary files under `output_dir` (default `/home/jetson1/innate-os/recordings/stereo_benchmark_runs`).

### 10.5 Create lidar-tagged video/image for every model run from latest summary

```bash
python3 - <<'PY'
import json
import subprocess
from pathlib import Path

out_dir = Path("/home/jetson1/innate-os/recordings/stereo_benchmark_runs")
summary_files = sorted(out_dir.glob("stereo_depth_benchmark_*.json"))
if not summary_files:
    raise SystemExit("No benchmark summary JSON found.")
latest = summary_files[-1]
rows = json.loads(latest.read_text(encoding="utf-8"))
for row in rows:
    model = row["model"]
    run_bag = row["run_bag"]
    video = out_dir / f"{model}_lidar_tagged.mp4"
    image = out_dir / f"{model}_lidar_tagged.png"
    cmd = [
        "ros2", "run", "mars_cam", "stereo_depth_benchmark", "annotate",
        "--bag", run_bag,
        "--image-topic", "/mars/main_camera/left/image_raw",
        "--lidar-topic", row.get("lidar_topic", "/scan"),
        "--camera-info-topic", row.get("camera_info_topic", "/mars/main_camera/left/camera_info"),
        "--output-video", str(video),
        "--output-image", str(image),
        "--image-frame-index", "150",
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
print("Done.")
PY
```

### 10.6 Rank models by navigation stability first (P95/P99)

```bash
python3 - <<'PY'
import json
from pathlib import Path

out_dir = Path("/home/jetson1/innate-os/recordings/stereo_benchmark_runs")
latest = sorted(out_dir.glob("stereo_depth_benchmark_*.json"))[-1]
rows = json.loads(latest.read_text(encoding="utf-8"))

def val(row, key):
    v = row.get(key)
    return float("inf") if v is None else float(v)

rows = sorted(rows, key=lambda r: (
    val(r, "inference_latency_p95_ms"),
    val(r, "inference_latency_p99_ms"),
    val(r, "dropped_frames_pct"),
))

print("Model ranking (best first):")
for i, r in enumerate(rows, 1):
    print(
        f"{i}. {r['model']}: "
        f"P95={r.get('inference_latency_p95_ms')} ms, "
        f"P99={r.get('inference_latency_p99_ms')} ms, "
        f"FPS={r.get('inference_effective_fps')}, "
        f"Dropped={r.get('dropped_frames_pct')}%, "
        f"GPU P95={r.get('gpu_util_p95_pct')}%, "
        f"Power P95={r.get('power_p95_w')} W, "
        f"Throttle={r.get('thermal_throttling_detected')}, "
        f"RMSE={r.get('rmse_m')} m"
    )
PY
```

Use this ordering for navigation decisions:

1. `inference_latency_p95_ms`
2. `inference_latency_p99_ms`
3. `camera_to_costmap_latency_p95_ms` (if available)
4. `dropped_frames_pct`
5. `thermal_throttling_detected`
6. depth quality (`rmse_m`, `mae_m`, `% within 10cm`)
