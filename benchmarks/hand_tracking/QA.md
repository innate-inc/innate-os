# Validation — 14 September 2026

- Ruff checks and formatting passed; JavaScript syntax check passed.
- Initial seven protocol tests passed: immutable save/retry, invalid upload/path rejection,
  session-preserving freeze, tamper rejection, interrupted-take exclusion,
  no-detection pose fallback, and coverage/accuracy distinction.
- Browser flow checked using an injected canvas camera on a separate disposable
  localhost server. No access to the operator's webcam. Countdown → 8-second
  capture → playback review → save → next clip passed. Stop during recording
  disabled Keep; Redo returned to ready state. No browser warnings/errors logged.
- The saved synthetic clip decoded to 241 frames and completed both MediaPipe and
  RTMDet/RTMPose offline runs, including their warm-up paths. This fixture contains
  background and a counter, not a moving hand: no model accuracy or realistic
  tracking-speed conclusion follows from it.
- MediaPipe 1.0.1 aborted inside its native graph initialization on this Mac.
  MediaPipe 0.10.32 with an explicit CPU delegate completed replay with the same
  full v1 model file; requirements now pin that working runtime.
- OpenCV reported 10 threads despite a request for one. The runner retains the
  observed value. ONNX thread counts of zero mean runtime defaults. This is not
  a verified single-thread benchmark.
- The disposable test tab/server were closed. The user's recorder remains at
  http://127.0.0.1:8766 with camera off. Test data/results are under
  `/tmp/innate-hand-recorder-test*`, outside the operator dataset.

## Operator calibration evaluation

- All 12 complete webcam recordings decoded: 2,877 frames, approximately 96 s,
  640×480 and approximately 30 fps. Only the calibration session exists.
- Original video and metadata hashes are frozen in `data/manifest.json`. One
  equal millisecond timestamp in the empty clip is preserved as source PTS; only
  the model API timestamp advances by one millisecond. No frames are dropped.
- Raw images at eight prespecified times per clip were visually reviewed before
  evaluated outputs. Annotation and temporal-reference hashes are frozen. These
  are Codex visual labels, not human-verified truth. Overlap ambiguity and coarse
  box uncertainty are explicit; no finger-joint or metric-depth scores are claimed.
- Each model completed three separate-process CPU runs on the identical frozen
  frames and harness. Predictions are identical across repeats of each model.
  Rendering ran after all timing runs finished. Full results are in `RESULTS.md`.
- All **13 tests passed**, including tied timestamps, missing prediction failures,
  presence versus target localization, clipped/ambiguous boxes, direction
  abstention, and frozen-reference tamper detection. Ruff checks/formatting pass.
- Latency and motion plots were visually inspected. Overlay frames were checked
  against raw frames and decoded source timestamps. The H.264 comparison is
  1280×576 with 719 frames at 30 fps; playback does not represent inference speed.

Pending: fresh normal/challenging test sessions, human label verification,
3D candidates, live latency under load, and sim/physical robot integration.
No robot commands were sent.
