# Stereo Benchmark Results (Text Summary)

- Run folder: `/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_23_36_37_i8_vs_fast_foundation_20_30_48_i8__bag_20260911_005944__run_20260911_171416`
- Summary JSON: `stereo_depth_benchmark_20260911_171658.json`
- Regenerate command: `python3 /home/jetson1/innate-os/scripts/stereo_benchmark_postprocess.py --run-dir /home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_23_36_37_i8_vs_fast_foundation_20_30_48_i8__bag_20260911_005944__run_20260911_171416 --columns 3`

## Key Runtime Metrics

| Model | Infer P95 ms | Infer P99 ms | FPS | Dropped % | RMSE m | MAE m | GPU P95 % | Power P95 W | Max Temp C |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| classical_stereo | 43.6 | 50.0 | 7.99 | 47.55 | 1.495 | 1.046 | 30.9 | 7.63 | 55.6 |
| fast_foundation_23_36_37_i8 | 2552.7 | 2920.3 | 0.73 | 95.10 | 1.263 | 0.829 | 99.0 | 16.28 | 61.5 |
| fast_foundation_20_30_48_i8 | 1634.1 | 1845.5 | 0.96 | 94.69 | 1.217 | 0.826 | 99.0 | 14.63 | 61.8 |

## Distance Error Bins

### classical_stereo

| Distance Bin m | Count | Mean Abs Error m | P90 Abs Error m | P95 Abs Error m |
|---|---:|---:|---:|---:|
| 0.50-1.00 | 1278 | 0.283 | 0.509 | 0.535 |
| 1.00-1.50 | 1335 | 0.200 | 0.405 | 0.528 |
| 1.50-2.00 | 1654 | 0.501 | 1.027 | 1.243 |
| 2.00-3.00 | 3440 | 0.980 | 1.471 | 1.590 |
| 3.00+ | 2463 | 2.559 | 4.096 | 4.511 |
| 0.00-0.50 | 641 | 0.279 | 0.513 | 0.525 |

### fast_foundation_20_30_48_i8

| Distance Bin m | Count | Mean Abs Error m | P90 Abs Error m | P95 Abs Error m |
|---|---:|---:|---:|---:|
| 0.50-1.00 | 136 | 0.464 | 0.601 | 0.639 |
| 1.00-1.50 | 166 | 0.366 | 0.618 | 0.748 |
| 1.50-2.00 | 218 | 0.327 | 0.748 | 1.211 |
| 2.00-3.00 | 345 | 0.693 | 1.235 | 1.406 |
| 3.00+ | 206 | 2.294 | 3.661 | 4.322 |
| 0.00-0.50 | 51 | 0.379 | 0.493 | 0.496 |

### fast_foundation_23_36_37_i8

| Distance Bin m | Count | Mean Abs Error m | P90 Abs Error m | P95 Abs Error m |
|---|---:|---:|---:|---:|
| 0.50-1.00 | 173 | 0.439 | 0.596 | 0.615 |
| 1.00-1.50 | 163 | 0.314 | 0.564 | 0.704 |
| 1.50-2.00 | 201 | 0.280 | 0.623 | 0.999 |
| 2.00-3.00 | 407 | 0.710 | 1.378 | 1.542 |
| 3.00+ | 231 | 2.327 | 4.038 | 4.888 |
| 0.00-0.50 | 81 | 0.392 | 0.495 | 0.517 |

## Worst Frame Regions (By Mean Error)

| Model | Region (row,col) | Count | Mean Abs Error m | P95 Abs Error m |
|---|---|---:|---:|---:|
| classical_stereo | 1,1 | 27 | 3.124 | 4.758 |
| classical_stereo | 1,2 | 710 | 3.088 | 4.802 |
| classical_stereo | 1,3 | 721 | 3.027 | 4.791 |
| fast_foundation_20_30_48_i8 | 1,3 | 59 | 2.921 | 4.708 |
| fast_foundation_20_30_48_i8 | 1,2 | 50 | 2.794 | 3.667 |
| fast_foundation_20_30_48_i8 | 1,1 | 13 | 2.651 | 3.088 |
| fast_foundation_23_36_37_i8 | 1,2 | 74 | 3.097 | 6.504 |
| fast_foundation_23_36_37_i8 | 1,3 | 64 | 2.316 | 4.079 |
| fast_foundation_23_36_37_i8 | 1,4 | 16 | 2.308 | 4.027 |

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
