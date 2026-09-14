# Stereo Benchmark Text Export

- Source run folder: `/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529`
- Preferred summary JSON: `stereo_depth_benchmark_20260911_191719.json`
- This export intentionally excludes PNG/MP4 binaries.

## Regenerate visual artifacts

Run on the robot/workspace where the source run bag data exists:

```bash
/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/REGENERATE_ARTIFACTS.sh
```

## Source bags for this run

These are the rosbag directories backing this run's metrics/overlays:

- `/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/20260911_191529_classical_stereo`
- `/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/20260911_191626_fast_foundation_20_30_48_i8_trt`

Canonical input bag candidate:

- `/home/jetson1/innate-os/recordings/stereo_canonical_20260911_005944`

Bags are intentionally not committed here (they are too large).

If you want a portable backup, sync them to external storage:

```bash
rsync -a --info=progress2 /home/jetson1/innate-os/recordings/stereo_canonical_20260911_005944 <backup_root>/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/
rsync -a --info=progress2 /home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/20260911_191529_classical_stereo <backup_root>/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/
rsync -a --info=progress2 /home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/20260911_191626_fast_foundation_20_30_48_i8_trt <backup_root>/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529/
```

Use `SOURCE_BAGS.txt` in this folder as the authoritative source-bag pointer list.

## Included files

- `REGENERATE_ARTIFACTS.sh`
- `RESULTS_MANIFEST_ALL_MODELS.txt`
- `RESULTS_SUMMARY_ALL_MODELS.md`
- `SOURCE_BAGS.txt`
- `all_models_distance_error_distribution.csv`
- `all_models_frame_region_error_heatmap.csv`
- `all_models_performance_overview.csv`
- `stereo_depth_benchmark_20260911_191719.csv`
- `stereo_depth_benchmark_20260911_191719.json`
- `stereo_depth_benchmark_20260911_191719_distance_error_distribution.csv`
- `stereo_depth_benchmark_20260911_191719_frame_region_error_heatmap.csv`
