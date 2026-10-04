# ml workstream status (running log)

**FINAL (22:20 box time): all five deliverables landed.** Server RUNNING at http://innate52.local:8000
(= http://192.168.0.156:8000): `medium` = distilled Qwen3.5-4B, `low` = distilled Qwen3.5-0.8B, both FP8 + 3 MTP
drafts, generator v3 (core c877d92f6). Shipped generator also at `expressive/out/models/generator.pt` (md5 746e338f...).

## 1. Retargeting — done
- `ml/retarget.py` + `ml/retarget.json` (the matrix, retunable). Reachy features relative to rest (x -8 mm, z 3 mm):
  attend = -pitch/25°, rise = z/25 mm, askew = roll/25°, orient = yaw + body (deg), advance = 5·x (m),
  approach = x/15 mm - 0.3·pitch/25° (head up adds lean-in: chosen by a labelled check, 65% sign agreement on 34
  clips vs 59% for x alone and 44% for +pitch), expand = ear up-ness (rest 15° -> 0, -10° -> +1, 150° -> -1),
  grip = interp(up-ness: -1 -> .02, 0 -> .15, +1 -> .5) + 0.6·max(0, ear flick - 3°)/30° (flick = |ear - 1.5 Hz lowpass|).
- sad1: expand -0.9, rise -1, attend -0.75 at the bottom (low, folded, gaze down); cheerful1 rise +0.68 with ±1 askew
  sways; amazed1 askew pinned -1 (Pollen's 20° roll). Plot: scratchpad/ml/retarget_sad_amazed_cheerful.png
- 104 real clips + 10,872 synthetic episodes retarget in 11 s on the 5090. Saturation: expand 27%/24% (real/syn) at
  up_deg 0 -> retuned to -10°.

## 2. Generator — done (`runs/generator/generator.pt` on the box = v3, 75% real / 25% synthetic, step 3250)
21.8M flow-matching transformer, N_DOF 8 / N_PLAN 9, trained on core `plan.extract` (1 Hz, 0.5 s) plans; held-out =
Binh's 12 emotions; batches mix real clips (x6 augmentation) and the synthetic library.

v2/v3 (after core's ENERGY_SCALE changes; v3 = core c877d92f6: tail key, orient-free energy): retarget.json also scales each channel's >1 Hz detail (`detail_gain`:
approach/expand .25, rise .5, askew .65, orient .3) so Reachy's ear/head jitter does not become arm jitter: real
arm-joint p95 speed 255 -> 151 deg/s, extracted clip energies now median 2.5 (p10 0.9, p90 6.7), in the planner's range.

Eval on the 12 held-out clips' true plans (3 seeds each), speeds through the core basis:

| | head p95 deg/s | arm joint p95 deg/s | >1 Hz detail-spectrum err | rms to real | held-out loss |
|---|---|---|---|---|---|
| real clips | 29.2 | 151 | - | - | - |
| **generator v3 (shipped)** | 19.9 | 120 | **0.68** | 0.15 | 0.081 |
| plan played directly | 14.7 | 90 | 0.82 | 0.13 | - |
| procedural liveliness (core c877d92f6) | 35.4 | 122 | 0.83 | 0.18 | - |
| generator v2 (previous core) | 22.8 | 121 | 0.67 | 0.18 | 0.092 |
| generator v2, 50/50 mix | 20.5 | 114 | 0.69 | 0.15 | 0.083 |

On 60 recipe-derived SERVING plans (2 Hz, 0.25 s): plan-tracking error 0.028 (range-normalised RMS of the 1 Hz
posture vs the plan), head p95 31 deg/s (plan direct 24, procedural 32), arm p95 154 deg/s.
Identification of the true clip from its true plan is 100% top-1 (12-way and 85-way) for every method, plan included:
saturated in MARS space, so speeds and the detail spectrum carry the signal.
Latency, 6 s clip, 8 Euler steps, CFG 1.5: GPU 14 ms; CPU Ryzen 9900X 240-260 ms on 4 threads, 126-160 ms on 12;
CPU M1 Pro 198 ms on 4 threads, 274 ms on 1 (4 steps: 103 ms, 2 steps: 50 ms). Expect the Orin Nano's A78AE cores to
be 2-3x slower than the M1 (~0.5 s): fine for a gesture, the 5090 path is 14 ms.
v1 (before the energy-scale change, no detail gains): same ranking; real-heavy mixes matched speed/spectrum best,
synthetic-heavy ones tracked plans best, 75/25 was the best compromise both times.

## 3. Dataset authoring — done (`ml/distill_data/`)
452 families / 3,164 prompts (Codex xhigh) + 118 hand-curated seed rows = 3,282 rows, 40 val prompts (one per family,
spread over the 17 categories) with two independent teacher labels each. Validity: 67.9% first pass, 100% after <= 2
repair rounds (0 rows dropped); 0 rows hit the probe leak filter. Median 8 segments, 5.4 s, median energy 1.2,
median frozen share 0%; energy peaks >= 5 in 23% of rows. Recipes in batches of 14 with 2 repair rounds + lively calibration;
seed set 118/118 accepted (self-critique with the checker tool), read and spot-checked (cough snaps forward-down,
sad sinks low/folded/gaze down, nods oscillate p).

Teacher (gpt-6-astra via Codex, 12 independent takes per probe, frozen prompt + house style): probes 0.90
(OOD-core 0.875, skill 0.92); real held-out clips from its recipes: top-1 17%, mean rank 3.9 (chance 8.3% / 6.5); misses only ecstatic (0.08) and jumping for joy (0.17), both on the E >= 5 threshold
(teacher peaks at E 4-4.5) -> house style amended for the last 78 batches: "short peaks of 5-10 on bursts".
First-pass validity 69%: nearly every first-pass error is `osc` written with the gesture word instead of the letter
(`osc 2 sway .2 .8`; sway 135, nod 108, lean 105, bob 64, chatter 44, turn 38) — the frozen prompt's
"nod (p), bob (z), ..." line invites it. All repaired in 1 round. Suggest core's DSL accept those six aliases in osc.

## 4. Planners — done (`runs/planner-4b/merged`, `runs/planner-08b/merged` on the box)
SFT set: 3,478 rows after the leak filter (seed rows x3) + 6,482 input variants ("word." / sentence alone) = 9,960
train, 40 val. Unsloth LoRA r = 32 / alpha 32 on every linear incl. the Gated DeltaNet mixers (4B: 200 layers, 72 of
them in_proj_qkv / in_proj_z / out_proj; 0.8B: 150 / 54), answer-only loss, 2 epochs, lr 1e-4, bs 32, best val loss
kept: 4B 0.8165 (step 624, 79 min at 7.3 s/step), 0.8B 0.9053 (step 600, ~25 min). Merge: the PEFT in-memory merge
of the 4B was OOM-killed twice by host RAM on the shared box, so `train.merge` now folds the LoRA into a copy of the
base checkpoint shard by shard (peak RAM one shard; vision tower + MTP head kept; refuses if any pair misses or no
weight changes): 200 layers merged, max weight change 1.3e-2.

18 core probes x 12 samples at T 0.7, plan agreement = mean per-descriptor Pearson r vs the teacher's first label on
the 40 val prompts (teacher's own second label = ceiling 0.77), real clips = greedy recipe for each of the 12 held-out
emotions -> 5 training-style plans -> generator -> rank of the true clip among the 12 (chance 8.3% / 6.5):

| | probes | OOD-core | skill | valid | agree | real top-1 / rank |
|---|---|---|---|---|---|---|
| teacher (gpt-6-astra) | 0.90 | 0.875 | 0.92 | 100%* | 0.77 (self) | 17% / 3.9 |
| 4B bf16 | 0.80 | 0.82 | 0.78 | 100% | 0.66 | 17% / 4.1 |
| 4B FP8 (served) | 0.80 | 0.82 | 0.78 | 99.6% | 0.66 | 23% / 4.1 |
| 0.8B bf16 (served FP8) | 0.63 | 0.65 | 0.62 | 99.2% | 0.59 | 17% / 5.0 |
| pilot 0.8B (1 epoch, half data) | 0.44 | 0.43 | 0.46 | 97% | 0.49 | - |

*teacher after <= 2 repair rounds; students are scored without retries (the server retries twice at T 0.7).
Per probe (teacher / 4B / 0.8B): the 4B passes 12 of 18 at >= 0.75. Misses: "shaking your head no" 0.00 (it nods with
p or sways k; MARS's no is a base `b` oscillation — the teacher does it 12/12), yawning 0.42 (mouth opens without
gaze up), ecstatic / jumping for joy 0.50 (energy peak < 5; the teacher itself scores 0.08 / 0.17 there),
"a big sneeze is coming" 0.75. The 0.8B loses the multi-phase ones (sleepy toddler 0.17, yawning 0.08, drunk 0.42),
like Binh's 0.8B.

## 5. Server — done, RUNNING
`expressive/ml/serve.sh` on the box (setsid; `serve.sh stop` kills vLLM's engine cores too). 16 prompts, n = 1,
`python -m ml.bench --url http://192.168.0.156:8000` from the Mac (wall = HTTP round trip incl. JSON):

| config | medium wall / planner | low wall / planner | generator |
|---|---|---|---|
| bf16 | 1041 / 993 ms | 318 / 293 ms | 15-18 ms |
| FP8 | 852 / 810 ms | 325 / 299 ms | |
| **FP8 + 3 MTP drafts (default)** | **671 / 622 ms** | **315 / 286 ms** | 18-23 ms |
| same, while another user's job holds 15 GB and 99% of the GPU | 1110 / 1075 ms | 550 / 507 ms | 29-38 ms |

n = 4 costs the same as n = 1 (one batched generator pass). Calling by hostname from macOS adds ~120 ms of mDNS per
request: use the IP. GPU shares are 0.3 / 0.12 with FP8 (0.4 / 0.15 bf16) so the server fits beside other jobs.
