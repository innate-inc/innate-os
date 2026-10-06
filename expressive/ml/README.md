# expressive/ml — the 5090 side of MARS expressive motion

Text → recipe (distilled planner) → plan → 25 Hz plan-space motion (flow generator) → Clip JSON, served over the LAN.
Everything here imports the pure core (`brain_client.expressive`: channels, dsl, plan, prompt, probes, motion) through
`_core.py`; nothing in this directory ships to the robot. Results and numbers: [STATUS.md](STATUS.md).

| piece | files | what |
|---|---|---|
| retargeting | `retarget.py`, `retarget.json` | Pollen's real clips + Binh's synthetic library → MARS plan space (the matrix is data) |
| generator | `generator/{model,data,train,sample,evaluate}.py` | 21.8M flow-matching transformer, plan → (T, 8) at 25 Hz |
| teacher data | `author.py`, `seed_prompts.tsv`, `distill_data/` | Codex (`gpt-6-astra`, xhigh) writes recipes with the frozen prompt; checked, repaired, lively-calibrated |
| planners | `distill/{sft,train,evaluate,common,mtp}.py` | Unsloth LoRA r = 32 on Qwen3.5-4B (`medium`) and 0.8B (`low`), Gated DeltaNet mixers included; fine-tuned MTP head |
| speech planners | `speech.py`, `distill/speech_eval.py`, `../eval/speech.py` | each spoken sentence (`prompt.speech_prompt`) -> a performance sized from its words; Codex-written replies + teacher, mixed into the same SFT; [SPEECH_PLANNER.md](../eval/SPEECH_PLANNER.md) |
| service | `engine.py`, `server.py`, `serve.sh`, `bench.py` | FastAPI per CONTRACTS §7: `/generate-sparse`, `/generate-dense`, `/health` |

## Results (details and the full tables in STATUS.md)

| | probes (18) | OOD-core | skill | valid | plan agreement | real held-out clips top-1 / rank | planner latency (idle GPU) |
|---|---|---|---|---|---|---|---|
| teacher (gpt-6-astra, 12 takes) | 0.90 | 0.875 | 0.92 | 100% after repair | ceiling 0.77 | 17% / 3.9 | — |
| `medium` Qwen3.5-4B talk, FP8 + 3 tuned MTP drafts | 0.81 | 0.81 | 0.81 | 99.6% | 0.67 | 12% / 4.8 | 0.41 s |
| `low` Qwen3.5-0.8B talk, FP8 | 0.62 | 0.57 | 0.66 | 100% | 0.58 | 8% / 5.0 | 0.31 s |

Held-out conversational beats (refuse, agree, wave, listen, ...): body checks 0.86 (4B) / 0.69 (0.8B) vs the
teacher's 0.97. Round-1 planners and the boost/MTP before-after are in STATUS.md.

Speech planners (`speech_prompt` per spoken sentence; 335 held-out rows from 108 unseen replies; details in
[SPEECH_PLANNER.md](../eval/SPEECH_PLANNER.md)):

| | physical cues | plain lines big | line A/B vs talk 4B | output tokens | latency p50 / p95, 1 / 8 in flight |
|---|---|---|---|---|---|
| teacher (gpt-6-astra) | 0.90 | 10% | 75% | 56 | — |
| talk 4B (`medium` until Mon) | 0.57 | 25% | — | 155 | 501 / 615, 768 / 1129 ms (async server) |
| **speech 0.8B** `planner-08b-speech` | 0.83 | 8% | **77%** | 57 | **116 / 211, 330 / 413 ms** |
| **speech 4B** `planner-4b-speech` | 0.89 | 8% | 75% | 54 | planner 175 / 340 ms (idle, offline); ~215 ms end to end est. |

Old-prompt metrics hold or improve (speech vs talk): 0.8B probes 0.63 vs 0.62; 4B probes 0.85 vs 0.81, talk beats
0.89 vs 0.86, "shaking your head no" 0.33 -> 0.83. Served on :8000 since Monday with `PLANNERS=speech serve.sh`
(`medium` = speech 4B, `low` = speech 0.8B, async server). Under GPU contention (another session's job on the card) the
0.8B measured 161 / 265 ms at 1 in flight, 478 / 791 ms at 8.

Generator (21.8M, 8 Euler steps, CFG 1.5): 14-23 ms on the 5090 per request; a 6 s clip costs ~0.2-0.26 s on 4 CPU
threads (M1 Pro / Ryzen 9900X). Its >1 Hz detail matches real motion better than the procedural liveliness layer
(spectrum error 0.68 vs 0.83) at real-like speeds (head p95 20 deg/s vs real 29, procedural 35).

## The box

`ssh jetson1@innate52.local`, workspace `W=/media/jetson1/nvme/theo/expressive` (root disk is full: keep everything
there): `envs/train` (torch, unsloth, trl, peft), `envs/serve` (vLLM), `repo/` (a mirror of this checkout's `expressive/`
and `brain_client/expressive/`, same relative layout), `runs/`, `data/`, `logs/`. From the Mac:

```bash
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude out/ expressive/ jetson1@innate52.local:$W/repo/expressive/
rsync -a --delete --exclude __pycache__ ros2_ws/src/brain/brain_client/brain_client/expressive/ \
      jetson1@innate52.local:$W/repo/ros2_ws/src/brain/brain_client/brain_client/expressive/
```

On the box: `source $W/env.sh` (HF_HOME, PYTHONPATH=$W/repo/expressive) and run from `$W`.

## Retrain

```bash
# 1. generator (~5 min): retargets everything once into data/corpus.pt (delete it after retuning retarget.json)
envs/train/bin/python -m ml.generator.train --out runs/generator/generator.pt --steps 6000 --bs 32 --real-frac 0.75
envs/train/bin/python -m ml.generator.evaluate --ckpt runs/generator/generator.pt      # speeds, spectrum, identification
envs/train/bin/python -m ml.generator.evaluate --ckpt runs/generator/generator.pt --bench

# 2. teacher data (on the Mac: Codex CLI; resumable, W = a scratch dir)
python -m ml.author families --work W && python -m ml.author recipes --work W --workers 24
python -m ml.author seed --work W && python -m ml.author val2 --work W && python -m ml.author probes --work W
python -m ml.author boost --work W        # 336 conversational rows (refuse, agree, listen, wave, ...), 56 held out
python -m ml.author build --work W        # -> ml/distill_data/{dataset,val,conv_val}.jsonl + report.json
python -m ml.speech replies --work S && python -m ml.speech teach --work S   # spoken replies + the teacher's beats
python -m ml.speech build --work S        # -> ml/distill_data/{speech,speech_dev,speech_val}.jsonl

# 3. planners (4B ~1 h, 0.8B ~15 min on the 5090), then score them (vLLM env)
envs/train/bin/python -m ml.distill.sft --out runs/sft_talk
envs/train/bin/python -m ml.distill.train --data runs/sft_talk --out runs/planner-4b-talk --model Qwen/Qwen3.5-4B
envs/train/bin/python -m ml.distill.train --data runs/sft_talk --out runs/planner-08b-talk --model Qwen/Qwen3.5-0.8B
VLLM_USE_FLASHINFER_SAMPLER=0 envs/serve/bin/python -m ml.distill.evaluate runs/planner-4b-talk/merged --fp8 --out runs/planner-4b-talk/eval_fp8.json
# MTP head on the planner's own answers (~15 min on the 4B; the planner frozen), served from the hard-linked copy
VLLM_USE_FLASHINFER_SAMPLER=0 envs/serve/bin/python -m ml.distill.mtp data runs/planner-4b/merged runs/sft/train.jsonl runs/planner-4b/mtp.jsonl
envs/train/bin/python -m ml.distill.mtp train runs/planner-4b/merged runs/planner-4b/mtp.jsonl runs/planner-4b/served
envs/train/bin/python -m ml.distill.evaluate --teacher W/teacher_probes.json      # the teacher on the same probes

# speech bundles (what PLANNERS=speech serves): the same recipe on the talk rows + the speech rows
envs/train/bin/python -m ml.distill.sft --out runs/sft_speech --speech repo/expressive/ml/distill_data/speech.jsonl
envs/train/bin/python -m ml.distill.train --data runs/sft_speech --out runs/planner-08b-speech --model Qwen/Qwen3.5-0.8B --eval-steps 50
envs/train/bin/python -m ml.distill.train --data runs/sft_speech --out runs/planner-4b-speech --model Qwen/Qwen3.5-4B --bs 8 --grad-accum 4 --eval-steps 50
# then ml.distill.mtp data/train per model as above (served = merged + its tuned MTP head) and
VLLM_USE_FLASHINFER_SAMPLER=0 envs/serve/bin/python -m ml.distill.speech_eval runs/planner-4b-speech/served --fp8 --spec-tokens 3 --out runs/speech_evals/s4.json
```

## Serve

```bash
repo/expressive/ml/serve.sh            # (re)starts in the background on :8000; `serve.sh stop` stops it
tail -f logs/server.log                # "ready: ..." after ~2 min (two vLLM engines + generator warm-up)
curl -s innate52.local:8000/health
curl -s innate52.local:8000/generate-dense -H 'content-type: application/json' \
     -d '{"prompt": "proud. You finally solved the puzzle.", "n": 1, "effort": "medium"}'
envs/train/bin/python -m ml.bench --url http://localhost:8000                     # 16 prompts, n = 1
```

Beside another GPU job, expect roughly 2x the planner latency. `serve.sh` serves `runs/planner-{4b,08b}-speech/served`
(`medium` / `low`; they plan spoken sentences and every emotion prompt); `PLANNERS=talk` rolls back to
`runs/planner-4b-talk/served` and `runs/planner-08b-talk/merged`, and `MEDIUM` / `LOW` override either; it also reads `GENERATOR`, `PORT` and `SERVE_FLAGS`. A `PORT` other than 8000 gets its
own pid and log file (`logs/server-<port>.{pid,log}`), so a test server never stops the live one. The default flags are
`--fp8 --spec-tokens 3` (FP8 weights + MTP drafts: the 4B goes from 1.04 s to 0.67 s per prompt, probes unchanged);
`SERVE_FLAGS=` serves exact bf16 weights. FlashInfer's sampler is disabled because it JIT-compiles with nvcc, which
the box lacks. On the Mac, call the server by IP (192.168.0.156): mDNS lookups add ~120 ms per request.

The blind eval harness uses the generator alone: `Generator(ckpt, device).generate_frames([plan_frames], [seed])` or
`ml.generator.sample.generate(plan_frames, seed)` (reads `expressive/out/models/generator.pt`, gitignored; override
with `MARS_FLOW_GENERATOR`). Feed it SERVING plans (`plan.frames` of a `dsl.variants` plan): it was trained on 1 Hz
extract plans on purpose and tracks 2 Hz serving plans to 0.028 range-normalised RMS.
