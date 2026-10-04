"""Unsloth LoRA SFT of a planner (loss on the assistant answer only), then a verified bf16 merge.

  python -m ml.distill.train --data runs/sft --out runs/planner-4b --model Qwen/Qwen3.5-4B
  python -m ml.distill.train --data runs/sft --out runs/planner-08b --model Qwen/Qwen3.5-0.8B

LoRA r = 32 on every linear layer: attention q/k/v/o, the MLPs, and the Gated DeltaNet token mixers (in_proj_qkv,
in_proj_z, out_proj), which Binh's served planners left frozen. Keeps the adapter with the best val loss and writes
<out>/merged (a full copy of the base checkpoint, MTP head included, with the LoRA folded in).
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

MTP_FILE = "model-mtp.safetensors"
TARGETS = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
    "in_proj_qkv",
    "in_proj_z",
    "out_proj",
]


def train(a: argparse.Namespace) -> None:
    from unsloth import FastLanguageModel  # noqa: I001 — unsloth must patch transformers before it is imported
    from unsloth.chat_templates import train_on_responses_only
    from datasets import load_dataset
    from trl import SFTConfig, SFTTrainer

    model, tok = FastLanguageModel.from_pretrained(a.model, max_seq_length=a.max_len, load_in_4bit=False, dtype=None)
    model = FastLanguageModel.get_peft_model(
        model,
        r=a.r,
        lora_alpha=a.r,
        lora_dropout=0.0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=0,
        target_modules=TARGETS,
    )
    lora = [n for n, _ in model.named_modules() if n.endswith(".lora_A")]
    print(
        f"[distill] {len(lora)} LoRA layers, {sum('linear_attn' in n for n in lora)} in Gated DeltaNet mixers",
        flush=True,
    )
    ds = load_dataset("json", data_files={"train": str(a.data / "train.jsonl"), "val": str(a.data / "val.jsonl")})

    def render(batch: dict[str, Any]) -> dict[str, list[str]]:
        return {"text": [tok.apply_chat_template(m, tokenize=False, enable_thinking=False) for m in batch["messages"]]}

    ds = ds.map(render, batched=True, remove_columns=ds["train"].column_names)
    trainer = SFTTrainer(
        model=model,
        processing_class=tok,
        train_dataset=ds["train"],
        eval_dataset=ds["val"],
        args=SFTConfig(
            output_dir=str(a.out),
            dataset_text_field="text",
            max_length=a.max_len,
            per_device_train_batch_size=a.bs,
            per_device_eval_batch_size=2,
            gradient_accumulation_steps=a.grad_accum,
            num_train_epochs=a.epochs,
            learning_rate=a.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            weight_decay=0.01,
            logging_steps=10,
            eval_strategy="steps",
            eval_steps=a.eval_steps,
            save_strategy="steps",
            save_steps=a.eval_steps,
            save_total_limit=2,
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
            bf16=True,
            optim="adamw_8bit",
            seed=0,
            report_to="none",
        ),
    )
    trainer = train_on_responses_only(
        trainer, instruction_part="<|im_start|>user\n", response_part="<|im_start|>assistant\n"
    )
    trainer.train()
    best = {
        "checkpoint": trainer.state.best_model_checkpoint,
        "eval_loss": trainer.state.best_metric,
        "log": trainer.state.log_history,
    }
    (a.out / "train_state.json").write_text(json.dumps(best, indent=1))
    print(f"[distill] best checkpoint {best['checkpoint']} (eval_loss {best['eval_loss']:.4f})", flush=True)
    model.save_pretrained(str(a.out / "adapter"))
    tok.save_pretrained(str(a.out / "adapter"))


def merge(base: str, adapter: Path, out: Path) -> None:
    """Merge the LoRA into a copy of the base checkpoint shard by shard (W += alpha / r * B A), so peak host RAM is
    one shard rather than the whole model (PEFT's in-memory merge of the 4B was OOM-killed on the shared box).

    Every adapter pair must land on a base tensor and the weights must change; otherwise a key mismatch would ship
    the base model as the planner. All other base tensors (vision tower, MTP head) are copied unchanged, so vLLM can
    still draft with MTP.
    """
    from huggingface_hub import snapshot_download
    from safetensors import safe_open
    from safetensors.torch import save_file

    config = json.loads((adapter / "adapter_config.json").read_text())
    if config.get("use_rslora") or config.get("use_dora") or config.get("fan_in_fan_out"):
        raise SystemExit("merge: rsLoRA, DoRA and fan_in_fan_out adapters need PEFT's merge")
    scale = config["lora_alpha"] / config["r"]
    with safe_open(str(adapter / "adapter_model.safetensors"), "pt") as f:
        lora = {k: f.get_tensor(k) for k in f.keys()}
    pairs = {
        k.removeprefix("base_model.model.").replace(".lora_A.weight", ".weight"): (
            lora[k],
            lora[k.replace("lora_A", "lora_B")],
        )
        for k in lora
        if k.endswith(".lora_A.weight")
    }
    root = Path(snapshot_download(base, allow_patterns=["*.json", "*.safetensors", "*.jinja", "*.txt"]))
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for extra in root.iterdir():
        if extra.suffix != ".safetensors":
            shutil.copy(extra, out / extra.name)
    merged, change, mtp = set(), 0.0, {}
    weight_map: dict[str, str] = {}
    for shard in sorted(root.glob("*.safetensors")):
        tensors = {}
        with safe_open(str(shard), "pt") as f:
            for key in f.keys():
                weight = f.get_tensor(key)
                if key.startswith("mtp."):
                    mtp[key] = weight
                    continue
                if key in pairs:
                    a, b = pairs[key]
                    updated = (weight.float() + scale * (b.float() @ a.float())).to(weight.dtype)
                    change = max(change, (updated.float() - weight.float()).abs().max().item())
                    weight = updated
                    merged.add(key)
                tensors[key] = weight
        save_file(tensors, str(out / shard.name), metadata={"format": "pt"})
        weight_map |= dict.fromkeys(tensors, shard.name)
        del tensors
    if mtp:
        # the MTP head gets its own file so ml.distill.mtp can swap a fine-tuned head in by hard-linking the rest
        save_file(mtp, str(out / MTP_FILE), metadata={"format": "pt"})
        weight_map |= dict.fromkeys(mtp, MTP_FILE)
    (out / "model.safetensors.index.json").write_text(json.dumps({"metadata": {}, "weight_map": weight_map}, indent=1))
    missing = sorted(set(pairs) - merged)
    if missing:
        raise SystemExit(f"merge: {len(missing)} LoRA pairs match no base tensor, e.g. {missing[:3]}")
    if change == 0:
        raise SystemExit("merge: weights unchanged after merging; refusing to save the base model as the planner")
    print(f"[distill] merged {len(merged)} LoRA layers (max weight change {change:.2e}) -> {out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.distill.train", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="Qwen/Qwen3.5-4B")
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--r", type=int, default=32)
    ap.add_argument("--bs", type=int, default=16)
    ap.add_argument("--grad-accum", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=1280)
    ap.add_argument("--eval-steps", type=int, default=25)
    ap.add_argument("--merge-only", action="store_true", help="just merge <out>/adapter into <out>/merged")
    a = ap.parse_args()
    if not a.merge_only:
        train(a)
    merge(a.model, a.out / "adapter", a.out / "merged")


if __name__ == "__main__":
    main()
