"""Distil the teacher (gpt-6-astra via Codex) into small local planners (Unsloth LoRA on the 5090).

1. sft.py       dataset.jsonl -> chat SFT set (OOD-probe leak filter, val split held out, input variants)
2. train.py     LoRA SFT, answer-only loss, best checkpoint by val loss, verified bf16 merge (+ the MTP head back)
3. evaluate.py  validity, probe pass rate, plan agreement with the teacher, latency
"""
