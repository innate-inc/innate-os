# Webcam control of the MARS arm

Research checked 14 September 2026. This is a shortlist for a fixed, ordinary RGB
webcam and one operator's hand. It is not a universal hand-pose leaderboard.
The [first calibration results](RESULTS.md) now compare MediaPipe and RTMPose on
the operator's recordings. The other candidates remain research proposals.

## Recommendation

Start with **MediaPipe Hand Landmarker** and **RTMDet + RTMPose-hand** as runnable
baselines. Compare **WiLoR** for camera-relative 3D motion, then evaluate a suitable
**HaMeR acceleration** if the first two fail the depth/rotation tests. Select using
the operator's held-out recordings and measured latency on the actual host.

For four-direction arm control we need a stable palm/wrist trajectory, reliable
presence detection, and quick recovery. Reconstructing all finger joints and a
detailed mesh is useful only if it improves those outcomes enough to justify its
latency. This is an engineering recommendation, not a claim that MediaPipe wins
the 2026 reconstruction benchmarks.

## Candidates and the 2026 evidence

| Candidate | Evidence and role | What remains to establish |
| --- | --- | --- |
| **MediaPipe Hand Landmarker** | Full-frame palm detector plus 21 landmarks, with temporal ROI reuse. Google's reference pipeline reports 17.12 ms CPU / 12.27 ms GPU on Pixel 6. Practical first baseline. | Mac calibration timing is now in RESULTS.md; Jetson timing, held-out occlusion performance, and depth proxy stability remain. Published phone timing is not transferable. |
| **RTMDet-nano + RTMPose-m hand** | Independently trained detector/keypoint pipeline available through RTMLib; hand pose model uses 256×256 crops and detector uses 320×320 input. Useful comparison against MediaPipe's 2D localization. | Mac calibration results now cover full pipeline cost and initial stability diagnostics. Held-out reliability and Jetson runtime remain; it does not supply metric camera depth. |
| **WiLoR, CVPR 2025, March 2026 fast path** | Joint hand localization/reconstruction approach. Official repository added half precision and depth pruning, reporting up to 1.6× speedup with `--fast`. Strong 3D candidate. | Reproduce the exact checkpoint/fast settings on our hardware; measure absolute wrist-motion stability. WiLoR-mini is a demo wrapper, not a separate trained model. |
| **Fast-HaMeR, March 2026 paper** | Distillation into lighter backbones; authors report 1.5× inference speedup and a 0.4 mm accuracy difference in their comparison. Directly relevant efficiency research. | Verify a specific released student checkpoint and its configuration; the demo download alone does not establish that we have a distilled student. Includes detector and crop costs in our benchmark. |
| **`fasthamer` / `fasterhamer`, 2026 Mac implementation** | Separate community CoreML/Apple Neural Engine port of HaMeR, not the Fast-HaMeR distillation paper. Maintainer reports ~30 FPS with two hands on an M4 MacBook Air. Exposes camera translation as well as hand-centered joints. | Our host is an **M1 Pro, 16 GB**, not an M4. Validate numerical changes, detector configuration, and runtime. It cannot run on a Jetson through its ANE backend. |

Primary sources: [Google's model guide](https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker),
[RTMLib hand pipeline source](https://github.com/Tau-J/rtmlib/blob/main/rtmlib/tools/solution/hand.py),
[MMPose hand model documentation](https://github.com/open-mmlab/mmpose/blob/main/docs/en/user_guides/inference.md),
[WiLoR repository](https://github.com/rolpotamias/WiLoR),
[WiLoR paper](https://arxiv.org/abs/2409.12259),
[Fast-HaMeR paper](https://arxiv.org/abs/2603.16444),
[Fast-HaMeR code](https://github.com/hunainahmedj/Fast-HaMeR),
[Mac HaMeR implementation](https://github.com/VimalMollyn/fasterhamer).

Model availability is part of selection. WiLoR's repository states restrictive
model terms and additional MANO/Ultralytics dependencies; HaMeR-derived paths also
require the applicable MANO rights. The Mac package explicitly prompts for prior
MANO acceptance. No acceptance has been asserted or bypassed by this experiment.
These paths are research candidates, not approved dependencies for shipping Innate.
Sources: [WiLoR license section](https://github.com/rolpotamias/WiLoR#license),
[HaMeR](https://github.com/geopavlakos/hamer),
[fasthamer setup](https://github.com/VimalMollyn/fasterhamer#install).

## Other 2026 research checked

**PAD-Hand (CVPR 2026)** refines pose sequences using physics-aware diffusion and
estimates physical consistency variance. It is relevant if reconstruction jitter
is the bottleneck. It is a sequence-refinement system, so we must establish causal
operation, buffering delay, and compute cost before using it in the live control
path. Its motion-recovery results do not establish low webcam-to-arm latency.
[Paper](https://arxiv.org/abs/2603.26068),
[official implementation](https://github.com/DominoAI-Lab/PAD-Hand-CVPR-2026).

**A2P (CVPR 2026)** addresses occluded two-hand reconstruction using structural
priors and penetration-free diffusion. That is a different priority from tracking
one free palm. Keep it as an occlusion research reference rather than a first
deployment candidate. [CVPR paper](https://openaccess.thecvf.com/content/CVPR2026/html/Han_From_2D_Alignment_to_3D_Plausibility_Unifying_Heterogeneous_2D_Priors_CVPR_2026_paper.html).

**EggHand (2026)** forecasts future egocentric hand poses. We want the robot to
follow observed user input; forecasting accuracy is not a substitute for tracking
accuracy. [Paper](https://arxiv.org/abs/2605.07642).

A **June 2026 hand-pose/occlusion study** evaluates WiLoR, HaMeR, WildHands, and
MediaPipe in object-interaction settings. Its participants/task differ from ours,
but it reinforces including actual occlusion and pose changes in our test set.
We are not borrowing its accuracy numbers for this operator.
[Study](https://arxiv.org/abs/2606.17427).

## Toward/away is the difficult axis

Google's normalized landmark `z` is relative to the wrist, and its world landmarks
are centered on the hand. Neither is a camera-to-hand range measurement. Reading
the world wrist `z` as distance would therefore be a conceptual error.
[Official output definitions](https://developers.google.com/edge/mediapipe/solutions/vision/hand_landmarker/python#handle_and_display_results).

For a fixed webcam, compare these depth estimators separately from the detector:

1. **Apparent palm scale:** use stable palm-bone lengths and calibrate a neutral
   pose; increasing image scale suggests approach. No absolute centimeters claim.
   Rotation, foreshortening, flexing, and occlusion can mimic distance changes.
2. **Calibrated geometric fit:** fit a personalized hand-size/pose model to image
   landmarks with calibrated camera intrinsics, recording fit residuals. This
   relies on shape and viewpoint assumptions; it is not independent depth truth.
3. **Model-estimated camera translation:** WiLoR/HaMeR camera-relative outputs,
   retaining their scale/camera assumptions. Check translation sign and stability,
   not just hand-centered finger accuracy.

Palm-scale net direction passed the initial calibration clips; rotation robustness
and the other depth estimators remain unvalidated. We can score
toward/away direction from reviewed video, but metric depth error needs measured
reference positions, a second calibrated view, or a depth reference. A good
Procrustes-aligned MPJPE removes translation/scale errors, so it cannot certify
the wrist trajectory needed for this controller.

## Recording and fair evaluation

The local recorder requests 640×480 at 30 fps, keeps the actual camera settings,
and stores unmirrored video, duration, clip/session IDs, instructions, and SHA-256
hashes. Preview/playback are mirrored. “Left” means the operator's left, which is
the opposite image-x direction in an unmirrored front-facing webcam.

Record 12 eight-second clips in each of three sessions:

- Calibration, normal lighting: choose thresholds, neutral position, smoothing,
  gains, and any personalized geometry here.
- Test, normal lighting: a fresh independent take, hand reset between clips.
- Test, changed lighting/background: repeat without retuning.

The clips cover no hand, a supported stationary hold, left/right, toward/away,
up/down, wrist rotation at fixed depth, partial object occlusion, disappearance
and re-entry, and fast movement. Raw capture is **4 min 48 sec** for all 36 clips;
allow roughly 8–12 minutes with countdowns and review. This is a personal pilot,
not population-level validation. Add longer holds/absence clips, both hands, a
second distracting hand/person, more sessions, and measured depth anchors before
deployment. Short clips cannot establish rare failure rates.

Freeze recordings before tuning. Inspect raw footage independently of model
outputs. Use a deterministic, condition-stratified sample for human presence,
bounding-box, and wrist/palm landmark labels; annotate dense visibility and motion
intervals where temporal metrics need them. Keep ambiguous/occluded points marked
as such. Prompts are **intended actions**, not verified per-frame truth. Model
predictions and other models must not become the reference labels.

| Question | Metric / reference |
| --- | --- |
| Does it find the correct hand? | Precision/recall with annotated boxes and a stated matching rule; misses stay in the denominator. Empty-frame false-positive rate reported separately. |
| Where is the hand? | Wrist/palm error in pixels and normalized by annotated hand size; PCK at frozen tolerances. Report detection failures separately and in end-to-end success. |
| Does it flicker or drift? | Visible-hand dropout durations; raw and filtered position jitter during reviewed still intervals; absence detections; reacquisition delay from reviewed entry time. Actual human movement is not estimator noise. |
| Does it infer the intended movement? | Left/right and toward/away direction accuracy from reviewed motion intervals; cross-axis leakage; false depth motion during wrist rotation. Report abstentions. |
| Is it responsive? | Sequential batch-1 pipeline p50/p95, cold acquisition and steady tracking separately; later live capture-to-output age, dropped frames, and robot response latency. |
| Does it fit the robot? | Memory, CPU/GPU use, thermal behavior and sustained rate under the normal ROS workload on Orin Nano. Mac timings do not answer this. |

Run the exact same decoded frames through every candidate, reset temporal state
between clips, retain source/weight/dependency hashes, and log all failures. For
performance use separate processes, identical source resolution, warm-up policy,
and three repeated runs per host. Model thresholds need not have equal numeric
values: scores are not calibrated across models. Tune on calibration, freeze,
then run test. Temporal methods must never read future test frames for a causal
comparison. Smoothing is a separate ablation with its added lag measured.

The included runners currently implement MediaPipe and RTMPose only. They output
raw predictions and offline latency/coverage diagnostics. They deliberately do
not produce accuracy, PCK, direction, or jitter scores before annotation. The
3D runners and task-level scorer are the next implementation step after reviewing
the recordings and selecting available checkpoints/hardware.

## Connecting to MARS after model selection

Provisional mapping, pending operator confirmation: hand toward webcam → arm up
(+z); away → down (−z); operator left/right → robot left/right (+y/−y in base_link).
Keep robot x and gripper/orientation fixed. Start at the measured current arm pose
when engaging, so calibration does not snap to a distant target.

Use a clutch, neutral calibration, dead zone, filtering, bounded velocity and
workspace, then Cartesian-to-joint IK and the existing joint stream. Stop on
released clutch, invalid/stale tracking, camera loss, hidden tab, or network loss.
Use a robot-side watchdog as well as sender-side checks. Reacquisition requires
deliberate re-engagement; it must not replay queued targets. Validate workspace,
IK reachability, limits, and dropout stopping in sim before a physical trial.

Repository evidence: `Manipulation` documents why repeated `move_to` calls are
rest-to-rest and serialize; `stream_joints` is the intended smooth teleop path.
Discrete moves are committed and cancellation does not interrupt them. A joint
stream's existing per-joint slew cap is not a Cartesian safety envelope.
See `ros2_ws/src/brain/brain_client/brain_client/robot/manipulation.py` and
`ros2_ws/src/brain/brain_client/brain_client/nodes/arm_sdk_server.py`.

No robot command path has been added or activated in this recording stage.
