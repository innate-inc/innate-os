# expressive/ml — the 5090 side of MARS expressive motion

Text → recipe (distilled planner) → plan → 25 Hz plan-space motion (flow generator) → Clip JSON, served over the LAN.
Everything here imports the pure core (`brain_client.expressive`: channels, dsl, plan, prompt, probes, motion) through
`_core.py`; nothing in this directory ships to the robot. Results and numbers: [STATUS.md](STATUS.md).

| piece | files | what |
|---|---|---|
| retargeting | `retarget.py`, `retarget.json` | Pollen's real clips + Binh's synthetic library → MARS plan space (the matrix is data) |
| generator | `generator/{model,data,train,sample,evaluate}.py` | 21.8M flow-matching transformer, plan → (T, 8) at 25 Hz |
| teacher data | `author.py`, `seed_prompts.tsv`, `distill_data/` | Codex (`gpt-6-astra`, xhigh) writes recipes with the frozen prompt; checked, repaired, lively-calibrated |
| planners | `distill/{sft,train,evaluate,common}.py` | Unsloth LoRA r = 32 on Qwen3.5-4B (`medium`) and 0.8B (`low`), Gated DeltaNet mixers included |
| service | `engine.py`, `server.py`, `serve.sh`, `bench.py` | FastAPI per CONTRACTS §7: `/generate-sparse`, `/generate-dense`, `/health` |

## Results (details and the full tables in STATUS.md)

| | probes (18) | OOD-core | skill | valid | plan agreement | real held-out clips top-1 / rank | latency (planner, alone / beside another GPU job) |
|---|---|---|---|---|---|---|---|
| teacher (gpt-6-astra, 12 takes) | 0.90 | 0.875 | 0.92 | 100% after repair | ceiling 0.77 | 17% / 3.9 | — |
| `medium` Qwen3.5-4B, FP8 + 3 MTP drafts | 0.80 | 0.82 | 0.78 | 100% (bf16) / 99.6% (FP8) | 0.66 | 17-23% / 4.1 | 0.62 s / 1.08 s |
| `low` Qwen3.5-0.8B, FP8 | 0.63 | 0.65 | 0.62 | 99.2% | 0.59 | 17% / 5.0 | 0.29 s / 0.51 s |

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
python -m ml.author build --work W                                            # -> ml/distill_data/{dataset,val}.jsonl

# 3. planners (4B ~1 h, 0.8B ~15 min on the 5090), then score them (vLLM env)
envs/train/bin/python -m ml.distill.sft --out runs/sft
envs/train/bin/python -m ml.distill.train --data runs/sft --out runs/planner-4b --model Qwen/Qwen3.5-4B
envs/train/bin/python -m ml.distill.train --data runs/sft --out runs/planner-08b --model Qwen/Qwen3.5-0.8B
VLLM_USE_FLASHINFER_SAMPLER=0 envs/serve/bin/python -m ml.distill.evaluate runs/planner-4b/merged --out runs/planner-4b/eval.json
envs/train/bin/python -m ml.distill.evaluate --teacher W/teacher_probes.json      # the teacher on the same probes
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

`serve.sh` reads `MEDIUM`, `LOW`, `GENERATOR`, `PORT` and `SERVE_FLAGS` from the environment. The default flags are
`--fp8 --spec-tokens 3` (FP8 weights + MTP drafts: the 4B goes from 1.04 s to 0.67 s per prompt, probes unchanged);
`SERVE_FLAGS=` serves exact bf16 weights. FlashInfer's sampler is disabled because it JIT-compiles with nvcc, which
the box lacks. On the Mac, call the server by IP (192.168.0.156): mDNS lookups add ~120 ms per request.

The blind eval harness uses the generator alone: `Generator(ckpt, device).generate_frames([plan_frames], [seed])` or
`ml.generator.sample.generate(plan_frames, seed)` (reads `expressive/out/models/generator.pt`, gitignored; override
with `MARS_FLOW_GENERATOR`). Feed it SERVING plans (`plan.frames` of a `dsl.variants` plan): it was trained on 1 Hz
extract plans on purpose and tracks 2 Hz serving plans to 0.028 range-normalised RMS.
