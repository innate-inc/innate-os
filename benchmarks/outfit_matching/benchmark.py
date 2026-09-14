#!/usr/bin/env python3
"""Synthetic outfit matching, without personal identity or biometric matching.

Run from any directory. Credentials are loaded from the repository .env without
printing them. No robot nodes are started and no actuation is performed.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LABELS = [f"OUTFIT_{i:02d}" for i in range(1, 16)]
CHOICES = LABELS + ["UNKNOWN", "UNCERTAIN"]
PROMPT = """Match a clothing ensemble in a query image against 15 labeled reference outfits.
All subjects are blank retail mannequins. Labels denote CLOTHING ENSEMBLES, not people.
Use only the garments: their colors, patterns, garment types, trousers/shorts, and footwear.
Ignore heads, faces, body shape, pose, background, and panel position.
Match the whole outfit, not just the top. If a visible garment contradicts every reference,
answer UNKNOWN. If insufficient clothing detail is visible to decide, answer UNCERTAIN.
Do not force a nearest match. The query can be any reference outfit or none of them.
Reference order carries no information. Each request is an independent task.
Return only JSON with exactly two fields: label and confidence.
label must be one of OUTFIT_01 through OUTFIT_15, UNKNOWN, UNCERTAIN.
confidence is a number from 0 to 1 for your decision; it is not assumed calibrated.
"""
SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": CHOICES},
        "confidence": {"type": "number"},
    },
    "required": ["label", "confidence"],
    "additionalProperties": False,
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def tiles(path):
    """Extract visually audited panel boundaries without retouching."""
    layout = json.loads((HERE / "panel_layouts.json").read_text())[path.stem]
    with Image.open(path) as source:
        source = source.convert("RGB")
        xs, ys = layout["x_edges"], layout["y_edges"]
        if source.size != (xs[-1], ys[-1]) or (len(xs) - 1) * (len(ys) - 1) != 20:
            raise ValueError("Panel layout must cover exactly 20 panels and the full image")
        for y1, y2 in zip(ys, ys[1:], strict=False):
            for x1, x2 in zip(xs, xs[1:], strict=False):
                yield source.crop((x1 + 2, y1 + 2, x2 - 2, y2 - 2))


def prepare():
    gallery = []
    cases = []
    asset_dir = HERE / "assets"
    (asset_dir / "gallery").mkdir(parents=True, exist_ok=True)
    (asset_dir / "queries").mkdir(parents=True, exist_ok=True)
    for i, tile in enumerate(tiles(asset_dir / "reference.png")):
        if i >= 15:
            break
        path = asset_dir / "gallery" / f"{LABELS[i]}.jpg"
        tile.thumbnail((320, 240), Image.Resampling.LANCZOS)
        tile.save(path, quality=85)
        gallery.append({"label": LABELS[i], "path": str(path.relative_to(HERE)), "sha256": digest(path)})
    for scene in ("low_angle", "distant"):
        for i, tile in enumerate(tiles(asset_dir / f"{scene}.png")):
            for size in (320, 160):
                case_id = f"{scene}_{size}_{i + 1:02d}"
                path = asset_dir / "queries" / f"{case_id}.jpg"
                tile.resize((size, size * 3 // 4), Image.Resampling.LANCZOS).save(path, quality=70)
                cases.append(
                    {
                        "id": case_id,
                        "condition": f"{scene}_{size}",
                        "outfit_index": i + 1,
                        "expected": LABELS[i] if i < 15 else "UNKNOWN",
                        "path": str(path.relative_to(HERE)),
                        "sha256": digest(path),
                    }
                )
    rng = random.Random(20260909)
    rng.shuffle(cases)
    for case in cases:
        order = list(range(15))
        rng.shuffle(order)
        case["gallery_order"] = order
    manifest = {
        "version": 1,
        "task": "synthetic_clothing_ensemble_matching",
        "seed": 20260909,
        "gallery": gallery,
        "cases": cases,
        "source_sheets": {name: digest(asset_dir / f"{name}.png") for name in ("reference", "low_angle", "distant")},
        "limitations": [
            "Synthetic mannequin images, not personal identity recognition.",
            "Generated perspective and distance are not metrically calibrated.",
            "Resizing cannot add detail to native generated tiles.",
            "Size variants and views share outfits and are not independent samples.",
        ],
    }
    dump(HERE / "manifest.json", manifest)
    print(f"Prepared {len(gallery)} reference outfits and {len(cases)} queries.")


def parse_prediction(raw):
    pred = json.loads(raw)
    if not isinstance(pred, dict) or set(pred) != {"label", "confidence"}:
        raise ValueError("Invalid prediction fields")
    if pred["label"] not in CHOICES:
        raise ValueError("Invalid label")
    conf = pred["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
        raise ValueError("Invalid confidence")
    return pred


def image_b64(item):
    return base64.b64encode((HERE / item["path"]).read_bytes()).decode("ascii")


def ordered_inputs(manifest, case):
    """Keep truth labels, source filenames, and unknown references out of requests."""
    items = [("text", PROMPT)]
    for index in case["gallery_order"]:
        item = manifest["gallery"][index]
        items.extend([("text", f"Reference outfit {item['label']}"), ("image", image_b64(item))])
    items.extend([("text", "QUERY IMAGE: match the clothing ensemble or reject/abstain."), ("image", image_b64(case))])
    return items


class Provider:
    def __init__(self, name, model, effort):
        self.name, self.model, self.effort = name, model, effort
        self.client = None
        self.proxy = None
        if name == "astra":
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY is missing")
            self.client = httpx.Client(timeout=120, headers={"Authorization": f"Bearer {key}"})
        else:
            for package in ("auth-client", "proxy-client"):
                sys.path.insert(0, str(REPO / "ros2_ws/src/cloud/clients" / package))
            from innate_proxy import ProxyClient

            self.proxy = ProxyClient()
            if not self.proxy.is_available():
                raise RuntimeError("Innate proxy credentials are missing")

    def close(self):
        if self.client:
            self.client.close()
        if self.proxy:
            self.proxy.close()

    def call(self, items):
        if self.name == "astra":
            content = [
                {"type": "input_text", "text": value}
                if kind == "text"
                else {"type": "input_image", "image_url": f"data:image/jpeg;base64,{value}", "detail": "high"}
                for kind, value in items
            ]
            body = {
                "model": self.model,
                "store": False,
                "reasoning": {"effort": self.effort},
                "max_output_tokens": 2048,
                "input": [{"role": "user", "content": content}],
                "text": {
                    "format": {"type": "json_schema", "name": "outfit_prediction", "strict": True, "schema": SCHEMA}
                },
            }
            response = self.client.post("https://api.openai.com/v1/responses", json=body)
            response.raise_for_status()
            data = response.json()
            raw = "".join(
                part.get("text", "")
                for output in data.get("output", [])
                for part in output.get("content", [])
                if part.get("type") == "output_text"
            )
            return {
                "raw_text": raw,
                "usage": data.get("usage", {}),
                "returned_model": data.get("model"),
                "response_id": data.get("id"),
                "finish": data.get("status"),
                "incomplete_details": data.get("incomplete_details"),
            }
        parts = [
            {"text": value} if kind == "text" else {"inlineData": {"mimeType": "image/jpeg", "data": value}}
            for kind, value in items
        ]
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 2048,
                "thinkingConfig": {"thinkingLevel": self.effort},
                "responseMimeType": "application/json",
                "responseJsonSchema": SCHEMA,
            },
        }
        with self.proxy.request_stream(
            "gemini", f"/v1beta/models/{self.model}:generateContent", json=body, timeout=120
        ) as response:
            response.read()
            response.raise_for_status()
            data = response.json()
        candidate = (data.get("candidates") or [{}])[0]
        raw = "".join(
            part.get("text", "") for part in candidate.get("content", {}).get("parts", []) if not part.get("thought")
        )
        return {
            "raw_text": raw,
            "usage": data.get("usageMetadata", {}),
            "returned_model": data.get("modelVersion"),
            "response_id": data.get("responseId"),
            "finish": candidate.get("finishReason"),
            "prompt_feedback": data.get("promptFeedback"),
        }


def run(args):
    load_dotenv(REPO / ".env")
    manifest_path = HERE / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["gallery"] + manifest["cases"]:
        if digest(HERE / item["path"]) != item["sha256"]:
            raise ValueError(f"Image hash mismatch: {item['path']}")
    config = {
        "provider": args.provider,
        "model": args.model,
        "effort": args.effort,
        "manifest_sha256": digest(manifest_path),
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "harness_sha256": digest(Path(__file__)),
        "max_output_tokens": 2048,
        "timeout_seconds": 120,
        "image_detail": "high" if args.provider == "astra" else "provider default",
        "started_utc": datetime.now(timezone.utc).isoformat(),
    }
    result_dir = HERE / "results" / args.run_name
    result_dir.mkdir(parents=True, exist_ok=True)
    config_path = result_dir / "config.json"
    if config_path.exists():
        prior = json.loads(config_path.read_text())
        if {k: v for k, v in prior.items() if k != "started_utc"} != {
            k: v for k, v in config.items() if k != "started_utc"
        }:
            raise ValueError("Run configuration changed; use a new --run-name")
    else:
        dump(config_path, config)
    results_path = result_dir / "predictions.jsonl"
    done = (
        {json.loads(line)["id"] for line in results_path.read_text().splitlines()} if results_path.exists() else set()
    )
    cases = manifest["cases"][: args.limit] if args.limit else manifest["cases"]
    provider = Provider(args.provider, args.model, args.effort)
    consecutive_errors = 0
    try:
        for case in cases:
            if case["id"] in done:
                continue
            items = ordered_inputs(manifest, case)
            result = {k: v for k, v in case.items() if k != "gallery_order"}
            result["provider"] = args.provider
            result["model"] = args.model
            start = time.monotonic()
            try:
                result.update(provider.call(items))
                result["latency_s"] = round(time.monotonic() - start, 4)
                result.update(parse_prediction(result["raw_text"]))
                result["status"] = "ok"
                consecutive_errors = 0
            except Exception as exc:
                # Never serialize credentials, HTTP request bodies, or raw exception messages.
                result.update(
                    status="error",
                    label=None,
                    confidence=None,
                    error_type=type(exc).__name__,
                    latency_s=round(time.monotonic() - start, 4),
                )
                if isinstance(exc, httpx.HTTPStatusError):
                    result["http_status"] = exc.response.status_code
                    try:
                        error = exc.response.json().get("error", {})
                        if isinstance(error, dict):
                            result["error_code"] = error.get("code")
                            result["error_type"] = error.get("type", type(exc).__name__)
                    except ValueError:
                        pass
                consecutive_errors += 1
            with results_path.open("a") as output:
                output.write(json.dumps(result) + "\n")
                output.flush()
            print(
                f"{args.provider}: {len(done) + 1}/{len(cases)} {case['id']}: "
                f"{result['label'] or result['error_type']} ({result['latency_s']:.1f}s)",
                flush=True,
            )
            done.add(case["id"])
            if consecutive_errors >= 3:
                raise RuntimeError("Stopped after three consecutive errors; inspect saved results")
    finally:
        provider.close()
    summarize(result_dir)


def metrics(rows):
    known = [r for r in rows if r["expected"] != "UNKNOWN"]
    unknown = [r for r in rows if r["expected"] == "UNKNOWN"]
    valid = [r for r in rows if r["status"] == "ok"]
    latency = sorted(r["latency_s"] for r in valid)
    return {
        "n": len(rows),
        "known_n": len(known),
        "unknown_n": len(unknown),
        "correct": sum(r["label"] == r["expected"] for r in rows),
        "known_correct": sum(r["label"] == r["expected"] for r in known),
        "unknown_correct": sum(r["label"] == "UNKNOWN" for r in unknown),
        "unknown_false_accepts": sum(r["label"] in LABELS for r in unknown),
        "abstentions": sum(r["label"] == "UNCERTAIN" for r in rows),
        "errors": len(rows) - len(valid),
        "median_latency_s": statistics.median(latency) if latency else None,
        "p95_latency_s": latency[math.ceil(0.95 * len(latency)) - 1] if latency else None,
    }


def summarize(result_dir):
    rows = [json.loads(line) for line in (result_dir / "predictions.jsonl").read_text().splitlines()]
    summary = {"all": metrics(rows)}
    for condition in sorted({r["condition"] for r in rows}):
        summary[condition] = metrics([r for r in rows if r["condition"] == condition])
    dump(result_dir / "summary.json", summary)
    with (result_dir / "predictions.csv").open("w", newline="") as output:
        fields = [
            "id",
            "condition",
            "outfit_index",
            "expected",
            "label",
            "confidence",
            "status",
            "latency_s",
            "returned_model",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--provider", choices=["astra", "gemini"], required=True)
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    run_parser.add_argument("--run-name", required=True)
    run_parser.add_argument("--limit", type=int)
    summary_parser = commands.add_parser("summarize")
    summary_parser.add_argument("run_name")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "run":
        run(args)
    else:
        summarize(HERE / "results" / args.run_name)


if __name__ == "__main__":
    main()
