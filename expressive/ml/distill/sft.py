"""dataset.jsonl -> chat SFT set (train.jsonl, val.jsonl) for the distilled planner.

  python -m ml.distill.sft --out runs/sft [--dataset ml/distill_data/dataset.jsonl --val ml/distill_data/val.jsonl]
  python -m ml.distill.sft --out runs/sft_speech --speech ml/distill_data/speech.jsonl

- rows whose prompt or family mentions an out-of-distribution probe concept, or whose bare concept is a probe's, are
  dropped, so the probes stay a generalisation test
- the val prompts are never trained on; val.jsonl holds their first teacher recipe
- every "word. sentence." prompt is also trained as "word." and as "sentence." alone (short prompts work)
- rows repeat by their ``weight`` (the hand-curated seed rows count 3x)
- ``--speech`` adds the speech planner's rows (``ml.speech``) as they are, leak-filtered but never split into variants
  (their quoted lines hold ". " too); their dev replies (speech_dev.jsonl beside them) join the val loss
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

from brain_client.expressive.dsl import check
from brain_client.expressive.prompt import messages

from ..author import leaks, probe_concepts

DATA = Path(__file__).parents[1] / "distill_data"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write(rows: list[dict[str, Any]], path: Path) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def build(dataset: Path, val_file: Path, out: Path, seed: int, speech: Path | None = None) -> None:
    rows = [r for r in _jsonl(dataset) for _ in range(int(r.get("weight", 1)))]
    val = _jsonl(val_file)
    held = {v["prompt"] for v in val}
    concepts = probe_concepts()
    kept = [
        r
        for r in rows
        if not leaks(r["prompt"], r.get("family", ""), concepts)
        and r["prompt"] not in held
        and check(r["recipe"]) is None
    ]
    variants: dict[tuple[str, str], dict[str, Any]] = {}
    for r in kept:
        word, _, rest = r["prompt"].partition(". ")
        if rest:
            variants.setdefault((word + ".", r["recipe"]), dict(r, prompt=word + "."))
            variants.setdefault((rest, r["recipe"]), dict(r, prompt=rest))
    spoken = [r for r in _jsonl(speech) if not leaks(r["prompt"], "", concepts)] if speech else []
    dev = _jsonl(speech.with_name("speech_dev.jsonl")) if speech else []
    train = kept + list(variants.values()) + [dict(r, source="speech") for r in spoken]
    random.Random(seed).shuffle(train)
    out.mkdir(parents=True, exist_ok=True)
    _write(
        [{"messages": messages(r["prompt"], r["idea"], r["recipe"]), "source": r["source"]} for r in train],
        out / "train.jsonl",
    )
    _write(
        [
            {"messages": messages(v["prompt"], v["labels"][0]["idea"], v["labels"][0]["recipe"]), "source": "val"}
            for v in val
        ]
        + [{"messages": messages(r["prompt"], r["idea"], r["recipe"]), "source": "speech_dev"} for r in dev],
        out / "val.jsonl",
    )
    print(
        f"[sft] {len(rows)} weighted rows -> {len(kept)} kept (leak filter + val + checker) + {len(variants)} input "
        f"variants + {len(spoken)} speech rows = {len(train)} train | {len(val)} + {len(dev)} speech val -> {out}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.distill.sft", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dataset", type=Path, default=DATA / "dataset.jsonl")
    ap.add_argument("--val", type=Path, default=DATA / "val.jsonl")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--speech", type=Path, help="speech.jsonl from ml.speech build (speech_dev.jsonl beside it)")
    a = ap.parse_args()
    build(a.dataset, a.val, a.out, a.seed, a.speech)


if __name__ == "__main__":
    main()
