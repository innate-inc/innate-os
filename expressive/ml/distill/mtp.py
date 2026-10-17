"""Fine-tune a planner's multi-token-prediction head on its own answers (the planner frozen), so vLLM's MTP drafts
are accepted more often (Binh: 2.3 -> 3.2 accepted tokens per step on the 4B, planner time -20-26%, outputs identical).

  envs/serve/bin/python -m ml.distill.mtp data  runs/planner-4b/merged runs/sft/train.jsonl runs/planner-4b/mtp.jsonl
  envs/train/bin/python -m ml.distill.mtp train runs/planner-4b/merged runs/planner-4b/mtp.jsonl runs/planner-4b/served

``data``: the planner's greedy answers to its own training prompts (probe and val prompts excluded) plus a third of
them again at T = 0.7. ``train``: matches vLLM's qwen3_5_mtp drafter, x = fc([norm_e(embed(tok[t+1])), norm_h(h[t])])
-> one full-attention decoder layer -> norm -> the shared lm_head predicts tok[t+2], h being the target's post-norm
hidden state; draft steps 2..k reuse the layer on its own output, so they are trained unrolled. OUT is a hard-linked
copy of the planner with a new model-mtp.safetensors.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from pathlib import Path
from typing import Any

from .train import MTP_FILE

Example = tuple[list[int], int]  # token ids, answer start


def make_data(planner: Path, sft_train: Path, out: Path, limit: int, gpu_share: float) -> None:
    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams

    from brain_client.expressive.probes import PROBES
    from brain_client.expressive.prompt import messages

    from .sft import DATA

    held = {p.prompt for p in PROBES} | {
        json.loads(line)["prompt"] for line in (DATA / "val.jsonl").read_text().splitlines()
    }
    prompts = sorted({json.loads(line)["messages"][1]["content"] for line in sft_train.read_text().splitlines()} - held)
    prompts = random.Random(0).sample(prompts, min(limit, len(prompts)))
    tok = AutoTokenizer.from_pretrained(str(planner))
    texts = [
        tok.apply_chat_template(messages(p), tokenize=False, add_generation_prompt=True, enable_thinking=False)
        for p in prompts
    ]
    llm = LLM(
        model=str(planner),
        max_model_len=2048,
        gpu_memory_utilization=gpu_share,
        max_num_seqs=64,
        enable_prefix_caching=True,
        limit_mm_per_prompt={"image": 0, "video": 0},
    )
    greedy = llm.generate(texts, SamplingParams(temperature=0, max_tokens=600))
    picked = random.Random(1).sample(range(len(texts)), len(texts) // 3)
    sampled = llm.generate(
        [texts[i] for i in picked], SamplingParams(temperature=0.7, top_p=0.95, max_tokens=600, seed=1)
    )
    rows = [{"prompt": t, "answer": o.outputs[0].text} for t, o in zip(texts, greedy, strict=True)]
    rows += [{"prompt": texts[i], "answer": o.outputs[0].text} for i, o in zip(picked, sampled, strict=True)]
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"[mtp] {len(rows)} answers to {len(texts)} prompts -> {out}", flush=True)


def train(a: argparse.Namespace) -> None:
    import torch
    import torch.nn.functional as F
    from safetensors.torch import load_file, save_file
    from torch import nn
    from transformers import AutoModelForImageTextToText, AutoTokenizer
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5DecoderLayer, Qwen3_5RMSNorm

    dev = "cuda"
    torch.manual_seed(0)
    tok = AutoTokenizer.from_pretrained(str(a.planner))
    target = AutoModelForImageTextToText.from_pretrained(str(a.planner), dtype=torch.bfloat16, device_map={"": dev})
    target.eval().requires_grad_(False)
    del target.model.visual  # never used here; frees ~0.8 GB on a GPU shared with the server
    torch.cuda.empty_cache()
    lm, cfg = target.model.language_model, target.config.text_config
    cfg._attn_implementation = "sdpa"
    embed, lm_head, rotary = lm.embed_tokens, target.lm_head, lm.rotary_emb
    width = cfg.hidden_size

    class Head(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.fc = nn.Linear(2 * width, width, bias=False)
            self.pre_fc_norm_embedding = Qwen3_5RMSNorm(width, eps=cfg.rms_norm_eps)
            self.pre_fc_norm_hidden = Qwen3_5RMSNorm(width, eps=cfg.rms_norm_eps)
            self.layers = nn.ModuleList([Qwen3_5DecoderLayer(cfg, cfg.layer_types.index("full_attention"))])
            self.norm = Qwen3_5RMSNorm(width, eps=cfg.rms_norm_eps)

        def forward(self, e: torch.Tensor, h: torch.Tensor, pos: tuple[torch.Tensor, torch.Tensor]) -> torch.Tensor:
            x = self.fc(torch.cat([self.pre_fc_norm_embedding(e), self.pre_fc_norm_hidden(h)], -1))
            return self.norm(self.layers[0](x, position_embeddings=pos))

    stock = load_file(str(a.planner / MTP_FILE))
    head = Head().to(dev)
    missing, unexpected = head.load_state_dict(
        {k.removeprefix("mtp."): v.float() for k, v in stock.items()}, strict=False
    )
    if missing or unexpected:
        raise SystemExit(f"mtp: head layout mismatch, missing {missing}, unexpected {unexpected}")

    def encode(row: dict[str, Any]) -> Example:
        prompt = tok(row["prompt"], add_special_tokens=False).input_ids
        answer = tok(row["answer"] + "<|im_end|>", add_special_tokens=False).input_ids
        return (prompt + answer)[: a.max_len], len(prompt)

    data = [encode(json.loads(line)) for line in a.data.read_text().splitlines()]
    random.Random(0).shuffle(data)
    val, train_set = data[:256], data[256:]

    def batch(items: list[Example]) -> tuple[torch.Tensor, torch.Tensor]:
        length = max(len(ids) for ids, _ in items)
        ids = torch.full((len(items), length), tok.pad_token_id or 0)
        mask = torch.zeros(len(items), length, dtype=torch.bool)
        for b, (seq, start) in enumerate(items):
            ids[b, : len(seq)] = torch.tensor(seq)
            mask[b, start : len(seq)] = True
        return ids.to(dev), mask.to(dev)

    def losses(ids: torch.Tensor, mask: torch.Tensor) -> tuple[list[torch.Tensor], list[float]]:
        with torch.no_grad():
            hidden = lm(input_ids=ids).last_hidden_state
            pos = torch.arange(ids.shape[1], device=dev)[None].expand(ids.shape[0], -1)
            cos, sin = rotary(hidden, pos)
        ce, acc = [], []
        for k in range(1, a.steps + 1):
            span = ids.shape[1] - 1 - k
            if span <= 0:
                break
            target_ids, keep = ids[:, k + 1 : k + 1 + span], mask[:, k + 1 : k + 1 + span]
            with torch.autocast("cuda", dtype=torch.bfloat16):
                y = head(embed(ids[:, k : k + span]), hidden[:, :span], (cos[:, :span], sin[:, :span]))
                logits = lm_head(y[keep]).float()
            ce.append(F.cross_entropy(logits, target_ids[keep]))
            acc.append((logits.argmax(-1) == target_ids[keep]).float().mean().item())
            hidden = y
        return ce, acc

    def evaluate() -> list[float]:
        head.eval()
        with torch.no_grad():
            accs = [losses(*batch(val[i : i + a.bs]))[1] for i in range(0, len(val), a.bs)]
        head.train()
        return [round(sum(x[k] for x in accs) / len(accs), 3) for k in range(len(accs[0]))]

    opt = torch.optim.AdamW(head.parameters(), lr=a.lr, weight_decay=0.0, betas=(0.9, 0.95))
    steps = math.ceil(len(train_set) / a.bs * a.epochs)
    weights = [1.0, 0.8, 0.6, 0.5, 0.4][: a.steps]
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / 50) * 0.5 * (1 + math.cos(math.pi * min(s, steps) / steps))
    )
    order = [
        i for e in range(math.ceil(a.epochs)) for i in random.Random(e).sample(range(len(train_set)), len(train_set))
    ]
    print(f"[mtp] {len(train_set)} train / {len(val)} val, per-draft top-1 before: {evaluate()}", flush=True)
    start = time.time()
    for s in range(steps):
        ce, _ = losses(*batch([train_set[j] for j in order[s * a.bs : (s + 1) * a.bs]]))
        loss = torch.stack([w * c for w, c in zip(weights, ce, strict=False)]).sum()
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(head.parameters(), 1.0)
        opt.step()
        sched.step()
        if (s + 1) % 50 == 0:
            print(f"[mtp] step {s + 1}/{steps} loss {loss.item():.3f} {time.time() - start:.0f}s", flush=True)
    print(f"[mtp] per-draft top-1 after: {evaluate()}", flush=True)

    a.out.mkdir(parents=True, exist_ok=True)
    for f in a.planner.iterdir():
        if f.name != MTP_FILE and not (a.out / f.name).exists():
            os.link(f.resolve(), a.out / f.name)
    tuned = {"mtp." + k: v.detach().to(torch.bfloat16).contiguous().cpu() for k, v in head.state_dict().items()}
    if set(tuned) != set(stock):
        raise SystemExit(f"mtp: tensor names changed: {set(tuned) ^ set(stock)}")
    save_file(tuned, str(a.out / MTP_FILE), metadata={"format": "pt"})
    print(f"[mtp] fine-tuned head -> {a.out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.distill.mtp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("data")
    d.add_argument("planner", type=Path)
    d.add_argument("sft", type=Path)
    d.add_argument("out", type=Path)
    d.add_argument("--limit", type=int, default=6000, help="prompts to answer")
    d.add_argument("--gpu-share", type=float, default=0.4)
    t = sub.add_parser("train")
    t.add_argument("planner", type=Path)
    t.add_argument("data", type=Path)
    t.add_argument("out", type=Path)
    t.add_argument("--epochs", type=float, default=1)
    t.add_argument("--lr", type=float, default=5e-5)
    t.add_argument("--steps", type=int, default=3, help="draft steps to unroll (= serving spec tokens)")
    t.add_argument("--bs", type=int, default=4)
    t.add_argument("--max-len", type=int, default=1024)
    a = ap.parse_args()
    if a.cmd == "data":
        make_data(a.planner, a.sft, a.out, a.limit, a.gpu_share)
    else:
        train(a)


if __name__ == "__main__":
    main()
