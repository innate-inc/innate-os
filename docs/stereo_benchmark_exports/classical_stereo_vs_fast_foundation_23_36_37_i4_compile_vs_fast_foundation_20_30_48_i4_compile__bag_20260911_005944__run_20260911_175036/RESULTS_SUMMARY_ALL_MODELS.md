# Stereo Benchmark Results (Text Summary)

- Run folder: `/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_23_36_37_i4_compile_vs_fast_foundation_20_30_48_i4_compile__bag_20260911_005944__run_20260911_175036`
- Summary JSON: `stereo_depth_benchmark_20260911_175318.json`
- Regenerate command: `python3 /home/jetson1/innate-os/scripts/stereo_benchmark_postprocess.py --run-dir /home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_23_36_37_i4_compile_vs_fast_foundation_20_30_48_i4_compile__bag_20260911_005944__run_20260911_175036 --columns 3`

## Key Runtime Metrics

| Model | Infer P95 ms | Infer P99 ms | FPS | Dropped % | RMSE m | MAE m | GPU P95 % | Power P95 W | Max Temp C |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| classical_stereo | 43.0 | 48.3 | 7.99 | 47.55 | 1.468 | 1.027 | 39.0 | 7.67 | 55.0 |
| fast_foundation_23_36_37_i4_compile | NA | NA | NA | 100.00 | NA | NA | 76.2 | 9.69 | 57.1 |
| fast_foundation_20_30_48_i4_compile | NA | NA | NA | 100.00 | NA | NA | 81.9 | 10.29 | 58.2 |

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
