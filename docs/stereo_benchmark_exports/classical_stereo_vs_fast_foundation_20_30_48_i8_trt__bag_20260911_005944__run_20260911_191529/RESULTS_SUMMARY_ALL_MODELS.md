# Stereo Benchmark Results (Text Summary)

- Run folder: `/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529`
- Summary JSON: `stereo_depth_benchmark_20260911_191719.json`
- Regenerate command: `python3 /home/jetson1/innate-os/scripts/stereo_benchmark_postprocess.py --run-dir /home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_20_30_48_i8_trt__bag_20260911_005944__run_20260911_191529 --columns 3`

## Key Runtime Metrics

| Model | Infer P95 ms | Infer P99 ms | FPS | Dropped % | RMSE m | MAE m | GPU P95 % | Power P95 W | Max Temp C |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| classical_stereo | 42.7 | 49.4 | 8.00 | 47.55 | 1.494 | 1.044 | 39.0 | 7.71 | 55.8 |
| fast_foundation_20_30_48_i8_trt | 1074.8 | 2073.7 | 1.31 | 90.82 | 1.313 | 0.889 | 99.0 | 13.10 | 59.7 |

## Distance Error Bins

### classical_stereo

| Distance Bin m | Count | Mean Abs Error m | P90 Abs Error m | P95 Abs Error m |
|---|---:|---:|---:|---:|
| 0.50-1.00 | 1303 | 0.309 | 0.513 | 0.557 |
| 1.00-1.50 | 1283 | 0.197 | 0.417 | 0.548 |
| 1.50-2.00 | 1615 | 0.502 | 1.010 | 1.260 |
| 2.00-3.00 | 3361 | 0.972 | 1.469 | 1.576 |
| 3.00+ | 2449 | 2.519 | 4.015 | 4.490 |
| 0.00-0.50 | 611 | 0.310 | 0.513 | 0.525 |

### fast_foundation_20_30_48_i8_trt

| Distance Bin m | Count | Mean Abs Error m | P90 Abs Error m | P95 Abs Error m |
|---|---:|---:|---:|---:|
| 0.50-1.00 | 266 | 0.486 | 0.785 | 0.849 |
| 1.00-1.50 | 208 | 0.337 | 0.618 | 0.673 |
| 1.50-2.00 | 357 | 0.383 | 1.039 | 1.407 |
| 2.00-3.00 | 531 | 0.750 | 1.536 | 1.690 |
| 3.00+ | 360 | 2.353 | 3.969 | 4.459 |
| 0.00-0.50 | 105 | 0.415 | 0.511 | 0.530 |

## Worst Frame Regions (By Mean Error)

| Model | Region (row,col) | Count | Mean Abs Error m | P95 Abs Error m |
|---|---|---:|---:|---:|
| classical_stereo | 1,1 | 27 | 3.373 | 5.289 |
| classical_stereo | 1,2 | 655 | 3.039 | 4.871 |
| classical_stereo | 1,3 | 741 | 2.976 | 4.702 |
| fast_foundation_20_30_48_i8_trt | 1,3 | 114 | 2.929 | 4.694 |
| fast_foundation_20_30_48_i8_trt | 1,2 | 76 | 2.571 | 4.078 |
| fast_foundation_20_30_48_i8_trt | 1,4 | 38 | 2.486 | 3.473 |

## Commit Strategy

- Commit text/CSV artifacts, not PNG/MP4 binaries.
- Keep the raw run bag folders available locally to regenerate visuals later.
- Run folders under recordings/ are gitignored in this repo.
- Export this bundle with --export-text-dir, then commit the exported folder.
- Use the helper script to regenerate all charts/overlays/videos in-place.

```bash
./REGENERATE_ARTIFACTS.sh
```

Suggested add list (from export folder):

```bash
git add <export_dir>/
```
