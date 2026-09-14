# Webcam hand tracking — first recording results

14 September 2026. **MediaPipe is the provisional choice for the first control
prototype on this Mac.** It followed the six recorded movement directions and
processed frames about 2.9× faster than the tested RTMDet + RTMPose configuration.
This is a calibration pilot comparing two runnable pipelines, not a ranking of
all 2026 models. WiLoR and HaMeR variants remain unevaluated.

Your 12 recordings are saved and usable: approximately 96 seconds, **2,877 decoded
frames**, 640×480, approximately 30 fps. They belong to one calibration session.
The normal and challenging test sessions have not been recorded yet.

## Measured comparison

| Measurement | MediaPipe | RTMDet + RTMPose |
| --- | ---: | ---: |
| Median processing time per frame | **14.4 ms** | 41.0 ms |
| 95th percentile processing time | **30.1 ms** | 68.7 ms |
| Hand present in reviewed positive samples | 85 / 85 | 85 / 85 |
| No detection in reviewed negative samples | 11 / 11 | 11 / 11 |
| Target localized in coarse reviewed boxes, IoU ≥ 0.3 | 81 / 81 | 81 / 81 |
| Same localization check at IoU ≥ 0.5 | 81 / 81 | 81 / 81 |
| Correct net direction: left, right, up, down, toward, away | 6 / 6 | 6 / 6 |
| Re-entry delay from first visible hand, two events | **34, 70 ms** | 71, 135 ms |

**Reference limitations:** Codex visually reviewed raw frames before inspecting
model predictions. These are coarse, AI-reviewed annotations, not human-verified
ground truth. Eight samples per clip were chosen at 0.5, 1.5, …, 7.5 seconds.
Four overlapping-hand samples were excluded from target localization, leaving
81; they still count for presence. These scores do not measure fingertip error,
continuous gesture accuracy, hand identity through occlusion, or metric depth.
The samples are correlated within one person's session, so perfect sampled
scores do not establish general reliability.

Timing values are the median of each statistic across three complete replays on
the **Mac M1 Pro, 16 GB**, using CPU backends. Timing includes synchronous
detection, cropping, pose estimation, and output conversion. Camera acquisition,
video decoding, rendering, network, and robot response are excluded. The 30 Hz
frame budget is 33.3 ms; MediaPipe's measured processing leaves more room for the
rest of the system, but actual webcam-to-arm latency is still unmeasured.

![Pipeline timing comparison](results/calibration-report/figures/latency.png)

| Replay | MediaPipe p50 / p95 | RTMDet + RTMPose p50 / p95 |
| --- | ---: | ---: |
| 1 | 14.55 / 30.34 ms | 43.53 / 84.18 ms |
| 2 | 14.38 / 30.07 ms | 40.69 / 68.73 ms |
| 3 | 14.37 / 30.02 ms | 41.02 / 66.70 ms |

Runs were sequential in separate processes: MP1, RTM1, RTM2, MP2, MP3, RTM3.
Predictions were identical across all three repeats of each pipeline; these are
timing repeats, not additional independent data. RTMPose's timing varied more.
This was a desktop run with background applications present, not an isolated
hardware benchmark. No Jetson or GPU timing has been measured.

The configurations also differ: MediaPipe VIDEO mode tracks at most one hand;
RTMDet performs detection every frame and RTMPose processes all native boxes.
The overlap clip therefore sometimes incurs two pose passes. The comparison is
between these complete configurations, not an architecture-only speed claim.
Both use the same full frames without ground-truth crops or prompt labels.
Pinned models, thresholds, library versions, warm-up policy, and observed thread
settings are retained in each run's `config.json`.

## What matters for controlling the arm

Both pipelines followed all six **net** movements. The lateral and vertical
signals agree closely. Toward/away currently uses apparent palm size, calculated
from two palm-bone lengths. It shows the expected sign here, but the two models
produce different scale magnitudes; neither is a camera-distance measurement.
Control gains will need calibration.

![Six movement traces](results/calibration-report/figures/motion.png)

The control diagnostic uses the largest visible hand's palm center: the mean of
landmarks 0, 5, 9, 13, and 17. Palm size is
`sqrt((distance(0,9)^2 + distance(5,17)^2) / 2)`. No extra smoothing was applied.
Direction compares median signals in 0.25–1.0 s and 6.5–7.75 s windows, with
abstention below 5 px for x/y or max(1 px, 1% initial size) for palm size.
This checks the beginning versus end, not reaction time or moment-to-moment
intent. In the unmirrored source, the operator's left is positive image x.

MediaPipe's output moved less during the hold clip: consecutive palm-center
steps had p95 **0.56 px**, versus **1.72 px** for RTMPose. Apparent palm-size
coefficient of variation was 0.77% versus 1.72%. These are output stability
diagnostics; actual hand and camera movement are included, so they are not pure
estimator-error measurements.

The re-entry clip contains two returns. MediaPipe detected the partially visible
hand one and two frames after first visibility; RTMPose took two and four frames.
Both detected by the first clearly usable open-palm frame. The reported delays
are video-time differences with roughly one-frame annotation uncertainty;
inference time is separate. Two events are too few for a reliability estimate.

Post-hoc inspection also found **one spurious RTMPose output** in the empty clip,
at frame 34 / 1.134 s, on the background near the plants. MediaPipe returned no
detections throughout that 241-frame clip. This event falls between the eight
preselected samples, explaining why both have 11/11 sampled negatives correct.
It is a visually confirmed failure example, not an unbiased false-positive rate
estimate. It reinforces requiring stable target acquisition before control.

[Inspect the spurious detection](data/review/overlay-empty-34.jpg).

Your occlusion take covers the target with the other hand. That is useful for
testing overlapping hands, but target identity becomes ambiguous; a largest-hand
selection can switch hands. We need an object occluder for the separate
single-hand occlusion test. Your rotation take is mostly an in-plane roll, which
does not strongly test the palm foreshortening that can imitate moving away.

[Watch the 24-second comparison](results/calibration-report/figures/comparison.mp4):
re-entry, overlapping hands, then fast movement. The white cross is the selected
palm center; all landmark outputs are shown. Playback is approximately source
speed, not the speed at which inference ran.

## Next recordings

In the [local recorder](http://127.0.0.1:8766/), record the two remaining sessions:

1. **2 · Test — fresh take, normal lighting**: another complete set of 12 clips.
2. **3 · Test — dimmer light / busy background**: repeat in a changed condition.

Keep the webcam fixed. For **Rotate**, turn your palm edge-on and back, like
opening a door, while keeping the wrist in place. For **Partly hide your hand**,
use a mug or book and keep the other hand outside the image. These two changes
test the unresolved depth and occlusion weaknesses.

The existing calibration manifest and predictions remain frozen. New sessions
need a new versioned manifest; do not overwrite this one. Use calibration only
to choose thresholds, smoothing, and gains, then evaluate fresh sessions without
retuning. Exact joint/depth accuracy additionally needs independently verified
labels or a measured reference. A subsequent on-screen control test must measure
live latency and stopping behavior before connecting the arm.

## Artifacts and verification

- [Machine-readable metrics](results/calibration-report/metrics.json), including
  sample-level overlaps, direction checks, entry events, and provenance hashes.
- [Frozen inputs](data/manifest.json), [reviewed annotations](data/review/annotations.json),
  and [temporal reference](data/review/temporal_reference.json).
- Six original run directories under `results/`, with predictions and timings.
- `analyze.py` verifies frozen references and identical frame coverage; missing
  predictions cannot disappear from the scoring denominator. `render_report.py`
  reproduces the plots and timestamp-checked overlay video.
- **13 tests passed**, plus Ruff checks. The output video was inspected and
  verified as H.264, 1280×576, 719 frames at 30 fps.

Recordings, models, and generated results remain local and Git-ignored. No robot
commands were sent during this experiment.
