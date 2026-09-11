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

The runner now always creates a **timestamped experiment folder** under `output_dir`.

Folder naming is dynamic from the YAML models used:

- model slug from enabled model names (in YAML order)
- canonical bag timestamp
- current run timestamp

Format:

`<model_slug>__bag_<canonical_bag_YYYYMMDD_HHMMSS>__run_<current_YYYYMMDD_HHMMSS>`

If the joined model slug is very long, it is shortened automatically while keeping the
same YAML-order prefix.

Example:

`classical_stereo_vs_fast_foundation_23_36_37_i8_vs_fast_foundation_20_30_48_i8__bag_20260911_005944__run_20260911_171416`

Outputs live inside that run folder:

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

## 8) Generate all-model analytics + overlays (single run folder)

This step uses the run summary JSON and processes **every model in the summary**
(which comes from the enabled models in your YAML run).

It creates:

- one combined distance-error distribution chart for all models
- one combined frame-region heatmap for all models
- one performance overview chart (latency, FPS, dropped frames, CPU/GPU/RAM, power, temp)
- lidar-vs-model overlays for each model
- one `N`-panel video that includes lidar reference + every model

Run:

```bash
python3 /home/jetson1/innate-os/scripts/stereo_benchmark_postprocess.py \
  --run-dir /home/jetson1/innate-os/recordings/stereo_benchmark_runs/<your_run_folder> \
  --columns 2
```

Optional: regenerate only the performance overview chart/CSV:

```bash
python3 /home/jetson1/innate-os/scripts/stereo_benchmark_perf_overview.py \
  --summary-json /home/jetson1/innate-os/recordings/stereo_benchmark_runs/<your_run_folder>/stereo_depth_benchmark_<stamp>.json \
  --output-dir /home/jetson1/innate-os/recordings/stereo_benchmark_runs/<your_run_folder> \
  --prefix all_models
```

Notes:

- `--columns` controls the N-panel grid layout.
- If `--summary-json` is omitted, the newest `stereo_depth_benchmark_*.json` in the run folder is used.
- The main postprocess script already includes performance overview generation.

## 9) Output files from all-model post-processing

In the run folder you should see:

- `all_models_npanel.mp4`
- `all_models_npanel_preview.png`
- `all_models_distance_error_distribution.png`
- `all_models_frame_region_error_heatmap.png`
- `all_models_distance_error_distribution.csv`
- `all_models_frame_region_error_heatmap.csv`
- `all_models_performance_overview.png`
- `all_models_performance_overview.csv`
- `classical_stereo_lidar_vs_model.mp4` / `.png`
- `<model_name>_lidar_vs_model.mp4` / `.png` for each enabled non-classical model
- `RESULTS_MANIFEST_ALL_MODELS.txt`

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

Current canonical run folder:

- `recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_23_36_37_i8_vs_fast_foundation_20_30_48_i8__bag_20260911_005944__run_20260911_171416`

Contains:

- per-model run bags and tegrastats logs
- benchmark summary JSON/CSV
- all-model charts (`all_models_distance_error_distribution.*`, `all_models_frame_region_error_heatmap.*`)
- `all_models_npanel.mp4` + preview PNG
- per-model lidar-vs-model overlays (`classical_stereo_lidar_vs_model.*`, `<model>_lidar_vs_model.*`)
- `RESULTS_MANIFEST_ALL_MODELS.txt`

## 13) Cleanup policy for output root

Keep the output root folder clean by storing artifacts in experiment folders only.

If you want to keep only one active run folder and archive older root entries:

```bash
python3 - <<'PY'
from pathlib import Path
from datetime import datetime
import shutil

root = Path('/home/jetson1/innate-os/recordings/stereo_benchmark_runs')
keep = root / 'classical_stereo_vs_fast_foundation_23_36_37_i8_vs_fast_foundation_20_30_48_i8__bag_20260911_005944__run_20260911_171416'
archive = root / f'old_results_archive_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
archive.mkdir(parents=True, exist_ok=True)

for p in sorted(root.iterdir()):
    if p == archive or p == keep:
        continue
    if p.name.startswith('old_results_archive_'):
        continue
    shutil.move(str(p), str(archive / p.name))
print(archive)
PY
```

If you already generated older pairwise artifacts in the same run folder, move them out:

```bash
python3 - <<'PY'
from pathlib import Path
from datetime import datetime
import shutil

run_dir = Path('/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_23_36_37_i8_vs_fast_foundation_20_30_48_i8__bag_20260911_005944__run_20260911_171416')
archive = run_dir / f'legacy_pairwise_artifacts_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
archive.mkdir(parents=True, exist_ok=True)

for p in sorted(run_dir.iterdir()):
    if p == archive or p.is_dir():
        continue
    if p.name.startswith(('all_models_', 'fast_foundation_', 'lidar_reference_all_models', 'stereo_depth_benchmark_')):
        continue
    if p.name in {'classical_stereo_lidar_vs_model.mp4', 'classical_stereo_lidar_vs_model.png'}:
        continue
    if p.name == 'RESULTS_MANIFEST_ALL_MODELS.txt':
        continue
    shutil.move(str(p), str(archive / p.name))
print(archive)
PY
```
