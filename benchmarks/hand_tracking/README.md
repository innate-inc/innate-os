# Hand tracking experiment

The [research review and experiment design](RESEARCH.md) cover evidence checked
on 14 September 2026, candidate models, depth ambiguities, and MARS integration.

Current stage: **initial calibration benchmark completed**. See [results](RESULTS.md)
for three replays of MediaPipe and RTMPose on 12 operator recordings. MediaPipe is
the provisional choice on this Mac. Fresh test sessions, human-verified labels,
3D candidate integration, and robot control trials are still pending.

## Record

From the repository root:

```sh
python3 benchmarks/hand_tracking/serve.py
```

Open <http://127.0.0.1:8766> in Chrome (or the Codex browser if camera permission
works there). Enable the webcam, choose your hand, and complete the 12 prompted
clips. Review each take; keep it or redo it. Record the calibration session first,
then the normal test and challenging test using the session dropdown. Keep the
same hand and fixed camera throughout. Both up/down and toward/away are collected
so the eventual motion mapping can change without losing this pilot dataset.

The preview is mirrored. The saved video is not. No audio, cloud calls, or robot
commands are used. Recordings are saved under `data/SESSION/RECORDING/` and ignored
by Git. A hidden tab or disconnected camera interrupts the take; redo it.
Reloading starts a new session at clip 1; old clips remain on disk. Do not reload
mid-session. Save acknowledgements include the on-disk path and video hash.

Once all three sessions are done, tell the agent **“recordings ready.”** The next
step is footage quality review and independent labeling before accuracy scoring.
You do not need to annotate all 21 finger landmarks for four-direction control.

## Prepare models and freeze data

Python 3.11 is used for the benchmark environment; the recorder needs only the
standard library. MediaPipe 1.0.1 aborted natively during initialization on this
M1 Pro; 0.10.32 passed the fixture replay with the same full v1 model bundle.
The pins are implementation/runtime versions; RTMPose's public
hand5 checkpoint is an older baseline, while MediaPipe uses full model bundle v1.

```sh
uv venv --python 3.11 benchmarks/hand_tracking/.venv
uv pip install --python benchmarks/hand_tracking/.venv/bin/python -r benchmarks/hand_tracking/requirements.txt
benchmarks/hand_tracking/.venv/bin/python benchmarks/hand_tracking/benchmark.py setup
benchmarks/hand_tracking/.venv/bin/python benchmarks/hand_tracking/benchmark.py freeze
```

`setup` fetches three model files from Google/OpenMMLab, recording the URLs and
observed SHA-256 hashes in `models/provenance.json`. Subsequent runs verify those
hashes. These are locally observed integrity hashes, not signed upstream hashes.
`freeze` refuses to overwrite an existing manifest or accept modified video. It
retains separate session splits and hashes metadata too. Review completeness and
duplicate sessions before freezing. No neighboring frames are randomly split.

## Run offline diagnostics

```sh
benchmarks/hand_tracking/.venv/bin/python benchmarks/hand_tracking/benchmark.py run --model mediapipe --split calibration --output benchmarks/hand_tracking/results/mediapipe-calibration-1
benchmarks/hand_tracking/.venv/bin/python benchmarks/hand_tracking/benchmark.py run --model rtmpose --split calibration --output benchmarks/hand_tracking/results/rtmpose-calibration-1
```

Use a new output directory on each run; completed/failed runs are not overwritten.
Run each model in a separate process. Frozen input hashes, model hashes, runtime
versions, thresholds, frame predictions and clip summaries are retained. MediaPipe
runs in VIDEO mode with one hand; RTMPose uses full-frame detection each frame.
RTMPose returns its native detections rather than silently choosing the largest
hand. The intended dataset is single-hand; the actual calibration occlusion take
contains overlapping hands. That difference is explicit in the results.
The RTMPose adapter disables RTMLib's whole-image pose fallback on zero detections.

**Coverage is not recall.** A detector can repeatedly find the wrong thing. The
runner emits `accuracy: null` until a reviewed reference exists. Offline timings
include synchronous detection, cropping, pose estimation and output conversion,
but exclude video decode, camera buffering, rendering, network and arm response.
Blank warm-up primes kernels; it is not a measured-hand warm-up or steady-state
tracking guarantee. First-frame acquisition is retained in each clip's timings.

The runner fails on decreasing decoded video timestamps or videos more than
one second shorter than their recorded wall duration. Browser stream settings are
requested, not guaranteed. Inspect decoded frames/PTS and actual rate before
comparing latency or temporal accuracy. Equal millisecond timestamps are retained
as source PTS, with the minimal increment applied only to the model API timestamp.
Retain original recordings if remuxing.

## Score reviewed samples and render the report

`analyze.py` uses SHA-frozen `data/review/annotations.json` and
`temporal_reference.json`. The current references were produced by Codex visual
review of raw frames before inspecting model output; they are **not human
verified**. They support coarse localization and net-direction diagnostics, not
finger-joint or metric-depth error. Raw inference summaries keep `accuracy: null`.

```sh
benchmarks/hand_tracking/.venv/bin/python benchmarks/hand_tracking/analyze.py --runs benchmarks/hand_tracking/results/mediapipe-calibration-1 benchmarks/hand_tracking/results/rtmpose-calibration-1 benchmarks/hand_tracking/results/rtmpose-calibration-2 benchmarks/hand_tracking/results/mediapipe-calibration-2 benchmarks/hand_tracking/results/mediapipe-calibration-3 benchmarks/hand_tracking/results/rtmpose-calibration-3 --output benchmarks/hand_tracking/results/calibration-report
benchmarks/hand_tracking/.venv/bin/python benchmarks/hand_tracking/render_report.py --metrics benchmarks/hand_tracking/results/calibration-report/metrics.json
```

Those outputs already exist for this pilot; use a fresh output name to rerun.
Rendering uses Matplotlib and `ffmpeg` and must run **after** timing finishes.
References, input recordings, and prediction files are not modified by analysis.

## Validation

```sh
uv pip install --python benchmarks/hand_tracking/.venv/bin/python pytest
benchmarks/hand_tracking/.venv/bin/python -m pytest benchmarks/hand_tracking/test_protocol.py benchmarks/hand_tracking/test_analysis.py
```

Tests cover recording integrity, retry/overwrite behavior, interrupted takes,
session splits, the empty-detection fallback, timestamp ties, honest metric
labeling, missed-frame denominators, localization versus presence, clipped boxes,
ambiguous targets, direction abstention, and frozen-reference integrity. Fixture
checks exercise the software; they are not model accuracy evidence.
