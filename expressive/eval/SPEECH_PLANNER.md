# Speech planner: each spoken sentence becomes a body performance

Theo: "can we get a small almost instant LLM that translates the sentences into things the robot can do?"
The robot's planner now performs every sentence it speaks. A distilled Qwen3.5-0.8B (`low`) or 4B (`medium`) reads
`speech_prompt(sentence, heard, before)` and writes the usual `{"idea", "recipe"}`. It decides the size from the
words: a visible but small beat for a plain line, little new when the feeling carries on, a whole-body gesture
for an emotional peak, and the physical cue the words name (yes nods, no shakes the base, hello/bye waves, "look
over there" turns the base, "come here" advances, "hmm" cants with the gaze up, laughter bobs).

**Status (Mon 2026-10-05):** served on :8000 (`low` = `planner-08b-speech` since 08:24, `medium` =
`planner-4b-speech` since ~13:40; async server).

| on 335 held-out lines from 108 unseen replies | talk 4B (served before) | **speech 0.8B** | **speech 4B** | teacher |
|---|---|---|---|---|
| physical cues the words call for | 0.57 | 0.83 | **0.89** | 0.90 |
| plain lines performed big / replies with 2 big lines in a row | 25% / 23% | 8% / 7% | 8% / 5% | 10% / 6% |
| line A/B vs talk 4B (GPT-5.5 strips, both orders; v8 / v9) | — | **76% / 70%** | **75% / 71%** | 75% / 73% |
| line A/B vs teacher (v8 / v9) | 25% / 27% | 46% / — | 49% / 55% | — |
| identify the spoken line out of 4 (chance 25%; v8 / v9) | 30% / 29% | 33% / 31% | 36% / 30% | 33% / 33% |
| whole replies with voice, A/B vs talk 4B (Gemini, 24; v8 / v9) | — | 3:11 / 2:11 | 1:12 / 4:11 | 1:8 / 3:8 |
| output tokens | 155 | 57 | 54 | 56 |
| latency p50 / p95, 1 in flight, idle card | 501 / 615 ms | **116 / 211 ms** | ~215 / ~380 ms (est.) | — |
| old prompts: probes / talk beats | 0.81 / 0.86 | 0.63 / 0.67 (talk 0.8B 0.62 / 0.69) | **0.85 / 0.89** | 0.90 / 0.97 |

- Per line, both speech planners fit the words 3 to 1 better than the talk planner, and as well as the teacher.
- Watching whole replies, Gemini still prefers the talk planner's constant motion, while calling it "restless, a
  gesture on every sentence". The side-by-side videos (Commands) are the honest way to decide.
- The speech 0.8B is the speech tier: it's 2x faster than the speech 4B, and the judges can't separate the two
  (line A/B 98:112, reply A/B 6:8 and 8:5).
- The speech 4B is better on every automatic speech metric and every old-prompt metric. "shaking your head no"
  goes 0.33 -> 0.83, so it replaces the talk 4B on `medium`.

## How it works

```
brain reply ─ transport.chat._split_sentences ─► sentence k
  ─ speech_prompt(sentence, heard if k == 0, before = sentence k-1) ─► POST /generate-dense {prompt, effort}
  ─► {"idea", "recipe"} ─► flow generator ─► clip, played as the sentence starts (it replaces the last one)
```

No new endpoint and no new prompt: the planner sees the frozen `SYSTEM` prompt and the frozen `speech_prompt` text.
The size policy exists only in the teacher's instructions; the students learned it from 8,730 answers. The same
bundles still plan every emotion prompt and emote tag: the 10,660 existing SFT rows are mixed in unchanged, so one
server serves both paths.

## Data (`ml/speech.py`)

1. **Exchanges** (`replies`). Codex (`gpt-6-astra`, xhigh) writes 30 exchanges per call for 27 scenes x 4 personas:
   friendly, plus 3 rotating from roast, grumpy, J-3SO droid, guard, playmate, carer, tutor and butler (the voices
   in `workspace/innate_agents`). Each exchange is what the person said (a speech-to-text transcript; empty for the
   16% of replies that MARS opens) and MARS's reply as sentences. The writer labels each sentence with a size
   (beat / continue / peak) and a physical cue (yes, no, hello, bye, look, come, think, laugh, none).
   - Scenes: small talk, facts, explaining, skill and navigation narration, memory, success, failure, jokes,
     laughter, comfort, greetings, farewells, agree, refuse, thinking, directing attention, excitement, strong
     negative moments, kids' games, security, proactive remarks, talking about itself, assistant chores, affection,
     asking back.
   - Plus spoken reactions (thank you, phew, ugh, oops, whoa). These were added because the coordinator's
     line-identification test showed they fail for every planner.
   - 108 calls, 26 in parallel, ~20 min: 3,210 replies, 2.85 sentences per reply.
2. **Rows** (`rows`).
   - Each reply is split with the robot's own `_split_sentences` + `split_emotes` + `split_tool_narration`
     (imported, not copied; `_core.py` puts `innate-llm` on the path).
   - Each sentence becomes `speech_prompt(sentence, heard, before)`: `heard` on a reply's first sentence, `before`
     on the rest. As a hedge, 10% of later sentences also carry `heard`, in case a caller passes it every time.
   - Whole replies are split off by hash, per scene: 4 per scene held out for eval (108 replies, 335 rows) and 1
     for the SFT val loss (75 rows). Training rows whose prompt equals a held-out prompt are dropped (2).
3. **Teacher** (`teach`).
   - ~24 rows per Codex call, grouped by reply in speaking order, so the teacher keeps an arc and avoids back-to-back
     peaks. It is told to answer each row only from what that row's prompt shows.
   - Its instructions are the frozen `SYSTEM` plus the teacher-only `TEACH_PROMPT`:
     - the size policy and the physical cues;
     - a distinct body word for each spoken feeling: thanks = a small bow, phew = held breath let go, ugh = clamp
       and slump, comfort = lean in low, sorry = shrink, oh/whoa = jolt back;
     - timing of ~0.37 s per spoken word (at least 1.2 s; peaks may settle 2 s longer);
     - ideas of at most 10 words, and only the keys a segment changes (output tokens are latency).
   - Checked with the core DSL checker plus length, idea and frozen-share checks, and repaired up to twice: 99.6%
     valid first pass, 100% after repair, 0 rows dropped. 405 calls in 22 min (two pools, 32 + 24 workers).
4. **Visible gain** (`visible`, applied by `build`).
   - The teacher sized plain lines right relative to peaks (gripper travel median 7 cm vs 19 cm), but on this body
     ±.3 is almost invisible. Watching whole replies with the voice, Gemini said the teacher "gestures once then
     freezes" and preferred the current talk planner 13:1.
   - `build` therefore maps every posture/stance value with `v -> sign(v)(1 - (1 - |v|)^1.8)` (b and d relative
     to their ranges; osc amplitudes too; grip and energy untouched). Small values grow most, order is kept and
     channel ends stay put.
   - Result: plain lines 12.5 cm / 14°, peaks 28 cm / 18°. Same reply-level judge: gained vs raw teacher 11:1.
   - `--gain 1` rebuilds the raw targets. `speech_val.jsonl` keeps `raw_recipe`; builds now keep it on every row
     (Sunday's `speech.jsonl` predates that).
5. **Build**: `distill_data/speech.jsonl` (8,750 rows), `speech_dev.jsonl` (75), `speech_val.jsonl` (335, with the
   writer's labels and the teacher's answer), `speech_report.json`.

| | speech rows (teacher) | existing emotion rows |
|---|---|---|
| answer tokens, median / p95 | **56 / 114** | 183 / 285 |
| idea tokens, median | 10 | 26 |
| segments / duration, median | 3 / 2.6 s | 8 / 5.4 s |
| writer's size labels | 7,132 beat, 426 continue, 1,598 peak | |
| writer's cue labels | 680 yes, 335 no, 118 hello, 129 bye, 476 look, 135 come, 295 think, 162 laugh | |

## Training

- **SFT** (`ml.distill.sft --speech`): 19,390 rows = the existing 10,660 talk rows (same leak filter, input variants
  and weights) + 8,730 speech rows (leak-filtered, 20 dropped; never split into "word." / "sentence." variants,
  since their quoted lines contain ". " too). The val loss is the 40 old val prompts + the 75 speech dev rows.
- **Recipe**: the existing one (`ml.distill.train`), LoRA r 32 on every linear layer incl. the Gated DeltaNet
  mixers, answer-only loss, 2 epochs, lr 1e-4, eval every 50 steps.
  - 0.8B: bs 16 x 2, ~45 min.
  - 4B: bs 8 x 4, ~2.4 h, beside the live server.
- **MTP head** (`ml.distill.mtp`): re-fine-tuned per model on its own answers to 6,000 training prompts (speech and
  emotion mixed) + 2,000 sampled. `served` = `merged` hard-linked + the tuned `model-mtp.safetensors`.

| | best step / steps | val loss (40 old val + 75 speech dev) | MTP per-draft top-1, stock -> tuned | legacy probes / talk beats / val agree |
|---|---|---|---|---|
| speech 0.8B | 1150 / 1212 | 0.736 | .60 .39 .33 -> **.84 .81 .80** | 0.63 / 0.67 / 0.65 (talk 0.8B: 0.62 / 0.69 / 0.58) |
| speech 4B | 1212 / 1212 | 0.627 | .71 .46 .38 -> **.86 .82 .80** | **0.85 / 0.89 / 0.68** (talk 4B: 0.81 / 0.86 / 0.67) |

The emotion prompts keep working (`ml.distill.evaluate`, same 18 probes x 12 samples, 56 held-out talk beats, 40 val
prompts):
- 0.8B, speech vs talk: probes 0.63 vs 0.62, val agreement 0.65 vs 0.58, talk agreement 0.61 vs 0.63, real-clip
  top-1 17% vs 8%, talk beat checks 0.67 vs 0.69. The talk beats have 4 prompts per beat, so ±1 prompt is ±0.25 on
  a beat: agree 0.50 -> 0.25, disagree 0.25 -> 0.75, goodbye 1.00 -> 1.00, hello 0.75 -> 0.50, pardon 1.0 -> 0.5,
  refuse 0.0 -> 0.25.
- Probes "nodding yes" 0.50 -> 0.83, "shaking your head no" stays 0.33.
- 4B, speech vs talk (both FP8 + MTP, as served): probes 0.85 vs 0.81 (OOD-core 0.82 vs 0.81, skill 0.88 vs 0.81),
  talk beat checks 0.89 vs 0.86, talk agreement 0.73 vs 0.71, val agreement 0.68 vs 0.67, real-clip top-1 17% vs 12%
  (rank 4.5 vs 4.8). "shaking your head no" goes 0.33 -> **0.83**: the probe the talk planners never learned, now
  that 335 spoken "no" lines shake the base. Per beat: refuse 0.75 -> 1.0, agree 0.5 -> 0.75, disagree 0.75 -> 0.5.
  The speech 4B is at least as good as the talk 4B on every legacy metric, so it can replace `medium`.
- One plausible systematic effect: the speech rows teach one-cycle nods and waves, and the emote checks want two
  swings.


## Metrics

On the 335 held-out rows (`ml.distill.speech_eval`, greedy, FP8 + 3 MTP drafts as served):

- **cues**: a body check per writer-labelled cue. One swing counts, since a spoken "No." gets a one-cycle shake.
  - yes: ≥1 attend swing of .12
  - no: ≥1 orient swing of 6°
  - hello / bye: rise ≥ .3 and an askew swing of .2
  - look: |orient| ≥ 12° or gaze ≤ −.25
  - come: advance ≥ 4 cm or approach ≥ .35
  - think: |askew| ≥ .25 and gaze ≥ .2
  - laugh: a rise swing of .1
- **size**: `reach` = the arm posture's largest excursion (a, x, z, k), `e_max` = the energy peak. `big` = reach ≥ .5
  or e_max ≥ 4. "Frantic" = the share of beat/continue lines performed big, and the share of replies with two big
  lines in a row.
- **agree**: per-descriptor Pearson r with the teacher's recipe.
- **tokens**: output tokens per answer.

Judges (all blind to the arm; `eval/speech.py`, `eval/speech_replies.py`):

- **line fit** (GPT-5.5 on a 2x4 key-frame strip, flow generator): the line and its context, rated 1-5.
- **line A/B**: two arms on the same line, shown in both orders; a win counts only when both orders agree.
- **identify**: which of 4 held-out lines the robot was saying (the coordinator's test; chance 25%).
- **reply-level** (Gemini 3.1 Pro on video with the voice): 24 held-out replies of ≥3 sentences, spoken back to back
  (macOS voice, speech-led timing: each sentence's clip starts with it and replaces the last one). Rated for fit,
  natural (1 = frantic, 5 = calm beats and real peaks) and expressive, plus A/B between arms in both orders.

## Results on the 335 held-out rows (108 replies never trained on)

All arms are planned greedily on the same `speech_prompt`s and served as FP8 + 3 MTP drafts. "talk 4B" is the
`medium` bundle :8000 served until Monday (`planner-4b-talk/served`). Renders use basis v8 (core at 8d3866277; the
Monday reruns render through a git-HEAD copy of the core so the other agents' in-progress speech-sway and basis edits
stay out), flow generator v3. The v9 rerun is its own section below.

| | cues | plain lines big | 2 big in a row | beat / peak reach | agree w/ teacher | s / word | tokens | valid |
|---|---|---|---|---|---|---|---|---|
| teacher (gpt-6-astra, gained) | 0.90 | 10% | 6% | .32 / .59 | 1 | 0.37 | 56 | 100% |
| teacher before the gain | 0.86 | 6% | 2% | .20 / .41 | | 0.37 | 56 | 100% |
| talk 4B (`medium` until Mon) | 0.57 | 25% | 23% | .44 / .54 | 0.42 | 0.60 | 155 | 98.8% |
| talk 0.8B (`low` until Mon 08:24) | 0.56 | 30% | 27% | .45 / .52 | 0.28 | 0.65 | 169 | 99.7% |
| **speech 0.8B** (`planner-08b-speech`) | **0.83** | **8%** | **7%** | .31 / .49 | 0.64 | 0.38 | **57** | 100% |
| speech 0.8B before the gain | 0.74 | 6% | 2% | .19 / .35 | 0.65 | 0.38 | 55 | 100% |
| **speech 4B** (`planner-4b-speech`) | **0.89** | 8% | **5%** | .31 / .52 | **0.75** | 0.37 | 54 | 100% |

Cue pass rates for the speech 0.8B (teacher): yes 1.00 (0.95), no 0.88 (0.82), hello 0.75 (1.0), bye 0.50 (0.75),
look 0.76 (0.95), come 1.00 (1.00), think 0.75 (0.75), laugh 1.00 (1.00). Rows per cue: 20 / 17 / 4 / 4 / 21 / 5 / 8 / 5.

Physical size of the clips (`eval/body.py`, median over 40 rows each):

| | beat: gripper travel / head range | peak: gripper travel / head range |
|---|---|---|
| teacher before the gain | 7.1 cm / 8.8° | 19.1 cm / 11.9° |
| teacher, gain 1.8 (trained) | 12.5 cm / 13.9° | 27.8 cm / 18.0° |
| teacher, gain 2.5 | 18.0 cm / 16.6° | 32.0 cm / 19.8° |
| talk 4B | 25.8 cm / 13.7° | 25.5 cm / 16.8° |

### Line level: GPT-5.5 judge on key-frame strips

Sunday run:

| arm | fit 1-5 | beat | continue | peak | identify (chance 25%) |
|---|---|---|---|---|---|
| teacher (gained) | 3.97 | 3.95 | 4.00 | 4.09 | 37% |
| talk 4B | 3.99 | 3.97 | 4.06 | 4.03 | 33% |
| speech 0.8B | 3.94 | 3.92 | 4.06 | 4.03 | 31% |

Monday rerun with the speech 4B, all arms rendered and judged in one pass (basis v8):

| arm (Monday rerun) | fit 1-5 | beat | continue | peak | identify (chance 25%) |
|---|---|---|---|---|---|
| teacher (gained) | 3.97 | 3.94 | 4.06 | 4.08 | 33% |
| talk 4B | 3.96 | 3.94 | 4.06 | 4.03 | 30% |
| speech 0.8B | 3.95 | 3.93 | 4.06 | 4.03 | 33% |
| **speech 4B** | 3.92 | 3.90 | 3.88 | 4.01 | **36%** |

A/B, "which motion fits the line better", both orders (a win needs both orders to agree):

| pair | wins | wins | position-split | share of agreed |
|---|---|---|---|---|
| talk 4B vs **speech 0.8B** | 60 | **205** | 66 | **77% speech 0.8B** |
| talk 4B vs teacher | 70 | 208 | 53 | 75% teacher |
| speech 0.8B vs teacher | 95 | 111 | 129 | 46 / 54 |
| talk 4B vs speech 0.8B before the gain | 82 | 193 | 56 | 70% speech |
| *Monday rerun:* talk 4B vs **speech 4B** | 65 | **199** | 67 | **75% speech 4B** |
| *Monday rerun:* talk 4B vs speech 0.8B | 62 | 196 | 73 | 76% speech 0.8B |
| *Monday rerun:* talk 4B vs teacher | 69 | 211 | 51 | 75% teacher |
| *Monday rerun:* speech 0.8B vs speech 4B | 98 | 112 | 125 | 47 / 53 |
| *Monday rerun:* speech 4B vs teacher | 102 | 108 | 125 | 49 / 51 |

The absolute fit ratings barely separate the arms; shown both motions, the judge picks the sized one 3 to 1. Identify
sits near chance for every arm on plain lines (a fitting beat for "Your meeting starts at three" is not unique). On
lines that carry a cue the teacher (before the gain) reached 46% vs 40% for talk 4B.

### The coordinator's 24-line identify test (`scratchpad/rawtest`, same option sets and judge)

| condition | correct of 48 |
|---|---|
| raw sentence, talk 4B | 21 (44%) |
| `speech_prompt`, talk 4B | 22 (46%) |
| emote translated by gpt-6-astra, then talk 4B | 20 (42%) |
| gpt-6-astra plans the raw sentence (the ceiling) | 26 (54%) |
| **`speech_prompt`, speech 0.8B** | **26 (54%)** |

The speech 4B is not in this test: its scratch data (plans, option sets) was lost with the scratch dir on Monday.

### Reply level: Gemini 3.1 Pro watches whole replies with the voice (24 replies, >= 3 sentences)

| arm | fit 1-5 | natural 1-5 | expressive 1-5 |
|---|---|---|---|
| teacher before the gain | 2.50 | 2.58 | 2.38 |
| teacher, gain 1.8 | 3.00 | 2.75 | 2.58 |
| teacher, gain 2.5 | 2.83 | 2.79 | 2.58 |
| talk 4B | 3.00 | 2.83 | 2.67 |
| speech 0.8B | 2.83 | 2.79 | 2.54 |

Rerun on Monday with all four arms rendered and judged in one pass (same 24 replies, same judge, new renders and
judgments; it reproduces the pattern, not the exact numbers, since one rating per video is noisy):

| arm (Monday rerun, basis v8) | fit 1-5 | natural 1-5 | expressive 1-5 |
|---|---|---|---|
| teacher, gain 1.8 | 2.79 | 2.71 | 2.67 |
| talk 4B | 2.92 | 2.75 | 2.58 |
| speech 0.8B | 2.79 | 2.83 | 2.67 |
| **speech 4B** | **2.96** | **2.88** | 2.62 |

| A/B (24 replies, both orders) | wins | wins | split |
|---|---|---|---|
| talk 4B vs teacher before the gain | 13 | 1 | 10 |
| teacher before vs after the gain | 3 | 11 | 10 |
| talk 4B vs teacher gain 1.8 | 10 | 5 | 9 |
| talk 4B vs teacher gain 2.5 | 8 | 5 | 11 |
| teacher gain 1.8 vs 2.5 | 3 | 4 | 15 |
| talk 4B vs speech 0.8B | 13 | 2 | 9 |
| speech 0.8B vs teacher gain 1.8 | 6 | 7 | 11 |
| *Monday rerun:* talk 4B vs speech 4B | 12 | 1 | 11 |
| *Monday rerun:* speech 0.8B vs speech 4B | 6 | 8 | 10 |
| *Monday rerun:* speech 4B vs teacher gain 1.8 | 0 | 9 | 14 |
| *Monday rerun:* talk 4B vs speech 0.8B | 11 | 3 | 10 |
| *Monday rerun:* talk 4B vs teacher gain 1.8 | 8 | 1 | 15 |

The two judges disagree in a consistent direction. Per line, the sized performance fits better (3:1). Over a whole
reply, Gemini still prefers talk 4B's constant motion, even though its reasons call it "repetitive / restless for
every sentence". It rates the two equally natural (2.83 vs 2.79). The gain fixed the teacher's "freezes" (11:3);
more gain (2.5) wins nothing at reply level and loses at line level (144:64 for 1.8). The rest of the gap is
probably duration: talk 4B's clips run ~2x the spoken line, so the robot is always mid-gesture when the next
sentence cuts in, while the sized clips settle at the line's end. Watch the side-by-side videos before deciding;
the gain is one number (`ml.speech build --gain`), and a 0.8B retrain takes ~45 min.


### Rerun on basis v9

The "lateral" agent's basis v9 (the arm now also swings to the robot's right; uncommitted `basis.json` "version": 9,
md5 5dfd6f27, with its `basis.py`) changes how every recipe looks, not what the planners write. Same answers, same
judges; renders through the HEAD core with only `basis.json` + `basis.py` swapped (the in-progress speech-sway edits
are left out).

| v9, line level | fit 1-5 | identify | A/B vs talk 4B | A/B vs teacher |
|---|---|---|---|---|
| teacher | 3.94 | 33% | 191:70 (73%) | — |
| talk 4B | 3.98 | 29% | — | 70:191 (27%) |
| speech 0.8B | 3.92 | 31% | 171:74 (70%) | |
| speech 4B | 3.96 | 30% | **184:74 (71%)** | 112:91 (55%) |

Speech 4B vs speech 0.8B: 108:87 (55% / 45%).

| v9, whole replies (24) | fit | natural | expressive | A/B vs talk 4B |
|---|---|---|---|---|
| teacher | 2.88 | 2.92 | 2.54 | 3:8 |
| talk 4B | 3.08 | 2.75 | 2.67 | — |
| speech 0.8B | 2.62 | 2.83 | 2.33 | 2:11 |
| speech 4B | 2.75 | 2.75 | 2.67 | 4:11 |

Speech 0.8B vs speech 4B 8:5; speech 4B vs teacher 3:7. The verdict holds on v9: per line the speech planners win
~70%, as the teacher does, and whole-reply A/B still leans to the constant motion of the talk planner. Side-by-sides
on both bases: `<scratch>/speech-planner/videos/*.4arms.{v8,v9}.mp4`.

## Serving: concurrent requests batch instead of queueing

The robot sends one request per spoken sentence on top of emote tags, and several robots or the studio share the
server. The old server ran one request at a time (a `threading.Lock` around planner + generator, the synchronous
`vllm.LLM`). The runtime agent measured four concurrent requests taking 0.31-1.41 s, and medium timing out under
load. `engine.py` now serves with:

- **`AsyncPlanner`**: vLLM's `AsyncLLM` per tier behind `async` FastAPI routes. Concurrent requests join the engine's
  continuous batch (`max_num_seqs` 16 per tier); retries stay per request.
- **`GeneratorBatcher`**: the flow generator on its own worker thread. Every request that queues while a batch runs
  goes into the next forward pass together. The noise is seeded per plan and padding is masked, so a clip does not
  depend on what it was batched with.
- **Startup**: the engines are built in the server's lifespan, since AsyncLLM binds to the running loop. The warm-up
  also runs 8 concurrent decodes and one sampled retry per tier, so the first real batch and the first retry don't
  hit Triton JIT.
- **API**: same endpoints, same JSON. The offline `Planner` (sync `vllm.LLM`) stays for the eval scripts.
- **`serve.sh`**: serves the speech bundles by default (`PLANNERS=talk` rolls back). A `PORT` other than 8000 gets its
  own pid and log file; before this, `PORT=8001 serve.sh` would first kill the pid in `logs/server.pid`, i.e. the live
  server.

### Latency: end to end from the Mac to the 5090 (`ml.bench`, 160 held-out speech prompts, /generate-dense, by IP)

Test server on :8001, Sunday evening, nothing else on the GPU but the live :8000 server. Each cell is p50 / p95 ms.

| server, bundle | 1 in flight | 4 in flight | 8 in flight | throughput at 8 |
|---|---|---|---|---|
| old sync server, talk 0.8B (`low` until Mon) | 326 / 434 | 1257 / 1413 | 2525 / 2976 | 3.1 req/s |
| old sync server, talk 4B (`medium` until Mon) | 466 / 625 | 1793 / 2040 | 3616 / 3909 | 2.2 req/s |
| async server, talk 0.8B | 329 / 429 | 414 / 534 | 525 / 698 | 14.7 req/s |
| async server, talk 4B | 501 / 615 | 593 / 765 | 768 / 1129 | 9.3 req/s |
| **async server, speech 0.8B** | **116 / 211** | **171 / 256** | **330 / 413** | 24.0 req/s |

The speech 4B could not be measured end to end: when it was ready, the card was shared with another session's
innate-nav server (11.5 GB, 0-99% util). Only 6 GB was free, and the 4B needs ~6.5-7 GB (4.9 GiB FP8 weights + CUDA
graphs + KV). Its planner-only time, measured offline at the end of training on the same otherwise idle card as the
0.8B row above (`speech_eval`, 64 single requests, FP8 + 3 tuned drafts):

| planner only, p50 / p95 ms | speech 0.8B | speech 4B | talk 4B (`medium` until Mon) |
|---|---|---|---|
| idle card, offline | 79 / 125 | 175 / 340 | 426 / 553 (planner part of the sync bench) |
| estimated /generate-dense from the Mac (+ ~38 ms generator + HTTP, as measured for the 0.8B) | 116 / 211 (measured) | ~215 / ~380 | 466 / 625 (measured) |

Under real contention (Monday noon, innate-nav on the same card swinging between 0 and 99% util), the speech 0.8B
alone on :8001 measured 1x **161 / 265**, 4x 348 / 566, 8x 478 / 791 ms (15.5 req/s), vs 116 / 211, 171 / 256 and
330 / 413 idle. Contention costs ~40% at 1 in flight and ~2x at 8. The 4B would scale the same way, from ~215 ms.

- With one request in flight, the async server costs the same as the old one (low 329 vs 326 ms).
- Under load, the old server queues: p50 grows linearly and throughput stays flat. The async server batches.
- The speech 0.8B is 2.8x faster than the talk `low` at 1 in flight: 57 output tokens instead of 169, and its tuned
  MTP head accepts 0.84 / 0.81 / 0.80 per draft (stock head 0.60 / 0.39 / 0.33).
- Sparse (planner only, no generator): 86 / 140 ms. In the dense 116 ms, the planner is 78 and the generator 17.

Planner-only knobs on the speech 0.8B (offline vLLM, one request at a time, 120 prompts, p50 / p95 ms):

| FP8, 3 tuned drafts (served) | no drafts | 5 drafts | bf16, 3 drafts | prefix cache off | max_tokens 96 |
|---|---|---|---|---|---|
| 78 / 141 | 120 / 222 | 78 / 147 | 81 / 141 | 78 / 139 | 79 / 118 |

- MTP is the lever (-35%). FP8 and the prefix cache are worth < 3 ms at 0.8B, since the 866-token system prompt
  prefills in a few ms.
- A 96-token cap trims the p95 but truncates the longest valid answers into retries, so it stays off.
- Shorter output was the big win, and it is already in the data: 10-token ideas and only the keys that change.
- Budget: a reply's first sentence has the TTS's 0.3-0.5 s first-audio delay; later sentences are prefetched while
  the previous one is spoken. 116 / 211 ms fits both, with room for 4 robots at once (171 / 256).

## Failure examples (speech 0.8B)

- **Punchlines kept small.** "At last, a professional." gets a small lift (z .47, E 1.5) where the teacher gathers
  and blooms open (z .85, x .76, E 4.5). Over the peak rows: reach .49 vs .59, big 37% vs 63%. Roast and droid
  deadpan lines are the hardest; the words alone rarely say "this is the punchline".
- **Uncertainty read as refusal.** "But I don't remember seeing them today." gets a base shake (`osc b 14`) where the
  teacher cants with the gaze aside. The think cue passes 6 of 8.
- **Looking at things named in passing.** "There's some beside your chair, too." keeps the previous comforting
  posture instead of glancing toward it (look 0.76 vs the teacher's 0.95).
- **Soft farewells.** "I'll leave you to it." and "I'll say goodbye now, then." get a soft lean, not a wave (bye 2
  of 4). Arguably fine for those words; a plain "bye" still waves.
- **Writer labels are noisy.** "Hello, familiar human." is labelled a plain beat but waves; "Unfamiliar just means
  I don't know who they are." is labelled "no". The teacher's own cue score (0.90) is the ceiling.
- **Inherited from the talk planners.** "shaking your head no" still nods sometimes (probe 0.33); the speech 4B
  fixes it (0.83).

## Commands

```bash
# data (on the Mac; Codex CLI; resumable, S = a scratch dir)
cd expressive
python -m ml.speech replies --work S --workers 26           # 108 calls, ~20 min
python -m ml.speech teach --work S --workers 32             # 405 calls, ~25 min (24 rows per call)
python -m ml.speech build --work S [--gain 1.8]             # -> ml/distill_data/speech*.jsonl
# The Sunday work dir was lost with a wiped scratchpad, and that build's speech.jsonl holds only the gained recipes
# (speech_val.jsonl keeps raw_recipe), so a new gain for TRAINING needs teach re-run (~25 min of Codex) into a new S;
# builds now keep raw_recipe on every row.
rsync expressive/ and brain_client/expressive/ to the box as in ml/README.md

# train (on the box, from $W, source env.sh)
envs/train/bin/python -m ml.distill.sft --out runs/sft_speech --speech repo/expressive/ml/distill_data/speech.jsonl
envs/train/bin/python -m ml.distill.train --data runs/sft_speech --out runs/planner-08b-speech --model Qwen/Qwen3.5-0.8B --eval-steps 50
envs/train/bin/python -m ml.distill.train --data runs/sft_speech --out runs/planner-4b-speech --model Qwen/Qwen3.5-4B --bs 8 --grad-accum 4 --eval-steps 50
for m in 08b 4b; do P=runs/planner-$m-speech
  VLLM_USE_FLASHINFER_SAMPLER=0 envs/serve/bin/python -m ml.distill.mtp data $P/merged runs/sft_speech/train.jsonl $P/mtp.jsonl --gpu-share 0.45
  envs/train/bin/python -m ml.distill.mtp train $P/merged $P/mtp.jsonl $P/served; done

# evaluate
VLLM_USE_FLASHINFER_SAMPLER=0 envs/serve/bin/python -m ml.distill.speech_eval runs/planner-08b-speech/served --fp8 --spec-tokens 3 --out runs/speech_evals/s08b.json
VLLM_USE_FLASHINFER_SAMPLER=0 envs/serve/bin/python -m ml.distill.evaluate runs/planner-08b-speech/merged --out runs/planner-08b-speech/eval.json
python -m ml.distill.speech_eval --teacher --out teacher.json                         # on the Mac, no GPU
uv run --extra flow python -m eval.speech --arm talk4b=talk4b.json --arm s08=s08b.json --pair talk4b,s08 --out out/speech_eval
uv run --extra flow python -m eval.speech_replies --arm talk4b=talk4b.json --arm s08=s08b.json --pair talk4b,s08 --out out/speech_replies
python -m ml.bench --url http://192.168.0.156:8001 --prompts ml/distill_data/speech_val.jsonl --effort low medium --concurrency 1 4 8
# (ml.distill.mtp data needs a 0.45 vLLM share for the bf16 4B; 0.3 left it no KV cache)

# faster renders: the eval scripts render whatever is missing and judge from cache, so the strips and videos can be
# made on the 5090 box's CPUs + EGL (minutes instead of hours on a busy Mac) and synced into the same --out dirs:
# a copy of the repo subset (brain_client, mars_sim_driver, mars_description, sim/{assets,props,environments},
# expressive, auth-client, webapp/js/expression/judge.js) with render/clip.py's FFMPEG = "ffmpeg", mujoco 3.10 on
# PYTHONPATH, then call eval.speech.build_media / eval.speech_replies.build_videos with CUDA_VISIBLE_DEVICES=
# MUJOCO_GL=egl (the flow generator on CPU; each EGL context takes ~0.3 GB of GPU). The macOS voice has to come from
# the Mac: run build_videos there once first (it writes the TTS cache) or copy videos/cache/tts.

# serve: test on another port (its own pid/log), then switch :8000
PORT=8001 repo/expressive/ml/serve.sh            # test server, own pid/log; PORT=8001 repo/expressive/ml/serve.sh stop
repo/expressive/ml/serve.sh                      # :8000, both speech bundles (what runs since Mon 2026-10-05 ~13:40)
PLANNERS=talk repo/expressive/ml/serve.sh        # :8000 rollback to the talk bundles
```

