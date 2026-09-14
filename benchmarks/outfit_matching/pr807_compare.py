#!/usr/bin/env python3
"""Compare PR #807's isolated garment matcher with Astra on synthetic outfits.

No face recognition, human identity, enrollment, or person tracking is evaluated.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import benchmark
import cv2
import numpy as np
from PIL import Image
from pr807.extracted_outfit import OutfitEmbedder, _best

ROOT = Path(__file__).resolve().parent
HERE = ROOT / "pr807"
MODEL_SHA256 = "7e49cb6b5a9b3fe3701a975900d5a98b80f5c3a5754208e46652d6bbcf29ce08"
# Manually selected once, before OSNet/Astra crop evaluation. The same rectangle
# applies to all twenty outfits within a view; no identity-specific crop tuning.
# Coordinates are in the existing 320x240 JPEG frame. Smaller frames scale them.
REGIONS = {"low_angle": (88, 0, 224, 238), "distant": (125, 60, 213, 183)}


def prepare():
    source = json.loads((ROOT / "manifest.json").read_text())
    for item in source["gallery"] + source["cases"]:
        if benchmark.digest(ROOT / item["path"]) != item["sha256"]:
            raise ValueError("Source image changed")
    gallery, cases = [], []
    (HERE / "crops" / "gallery").mkdir(parents=True, exist_ok=True)
    (HERE / "crops" / "queries").mkdir(parents=True, exist_ok=True)
    for original in source["gallery"]:
        path = HERE / "crops" / "gallery" / Path(original["path"]).name
        shutil.copyfile(ROOT / original["path"], path)
        gallery.append({**original, "path": str(path.relative_to(HERE))})
    for original in source["cases"]:
        scene = "distant" if original["condition"].startswith("distant") else "low_angle"
        with Image.open(ROOT / original["path"]) as image:
            factor = image.width / 320
            box = tuple(round(value * factor) for value in REGIONS[scene])
            # Crop only: preserve the existing decoded pixels in PNG, no upscaling,
            # sharpening, generative enhancement, or second JPEG compression.
            crop = image.crop(box)
            path = HERE / "crops" / "queries" / f"{original['id']}.png"
            crop.save(path)
        cases.append(
            {
                **original,
                "path": str(path.relative_to(HERE)),
                "sha256": benchmark.digest(path),
                "source_sha256": original["sha256"],
                "crop_xyxy": box,
                "crop_size": crop.size,
            }
        )
    manifest = {
        "version": 1,
        "task": "synthetic_clothing_ensemble_matching_manual_regions",
        "source_manifest_sha256": benchmark.digest(ROOT / "manifest.json"),
        "prepare_script_sha256": benchmark.digest(Path(__file__)),
        "seed": source["seed"],
        "regions_at_320": REGIONS,
        "gallery": gallery,
        "cases": cases,
        "limitations": [
            "Manual subject regions, no detector; this is not the full PR pipeline.",
            "Synthetic identical faceless mannequins; no human identity tested.",
            "All crop cases derive from the earlier test set; not new independent samples.",
        ],
    }
    benchmark.dump(HERE / "manifest.json", manifest)
    print("Prepared 15 unchanged references and 80 lossless region crops.")


def encode_image(model, item):
    image = cv2.imread(str(HERE / item["path"]), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Unreadable image")
    vector = model.embed(image)
    if vector is None or vector.shape != (512,) or not np.isfinite(vector).all():
        raise RuntimeError("Encoder returned no valid 512-dimensional vector")
    return vector


def run_osnet():
    manifest = json.loads((HERE / "manifest.json").read_text())
    source = json.loads((HERE / "source.json").read_text())
    weights = HERE / "osnet_x0_25_msmt17.onnx"
    if benchmark.digest(weights) != MODEL_SHA256:
        raise ValueError("Model digest mismatch")
    for item in manifest["gallery"] + manifest["cases"]:
        if benchmark.digest(HERE / item["path"]) != item["sha256"]:
            raise ValueError("Benchmark input changed")
    folder = HERE / "results" / "osnet-pr807"
    if (folder / "predictions.jsonl").exists():
        raise FileExistsError("Preserve the previous run; choose a new experiment directory before rerunning")
    folder.mkdir(parents=True, exist_ok=True)
    model = OutfitEmbedder(weights)
    start = time.perf_counter()
    gallery = [(item["label"], encode_image(model, item)) for item in manifest["gallery"]]
    startup_s = time.perf_counter() - start
    for _ in range(3):
        encode_image(model, manifest["gallery"][0])
    config = {
        **source,
        "model": "osnet_x0_25_msmt17",
        "provider": "local-onnxruntime-cpu",
        "manifest_sha256": benchmark.digest(HERE / "manifest.json"),
        "harness_sha256": benchmark.digest(Path(__file__)),
        "extracted_code_sha256": benchmark.digest(HERE / "extracted_outfit.py"),
        "host": platform.platform(),
        "machine": platform.machine(),
        "versions": {p: importlib.metadata.version(p) for p in ["numpy", "onnxruntime", "opencv-python-headless"]},
        "model_load_and_gallery_s": startup_s,
        "warmup_calls": 3,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "gallery": "one embedding per catalog outfit",
        "thresholds_tuned": False,
        "query_timing_includes": "image decode, crop embedding and gallery comparison; excludes model/gallery startup",
    }
    benchmark.dump(folder / "config.json", config)
    for i, case in enumerate(manifest["cases"]):
        start = time.perf_counter()
        vector = encode_image(model, case)
        ordered = [gallery[index] for index in case["gallery_order"]]
        label = _best(ordered, vector, source["outfit_accept"], source["outfit_margin"])
        ranked = sorted(((name, float(stored @ vector)) for name, stored in ordered), key=lambda p: p[1], reverse=True)
        latency = time.perf_counter() - start
        result = {
            **{k: v for k, v in case.items() if k != "gallery_order"},
            "label": label or "UNKNOWN",
            "confidence": None,
            "status": "ok",
            "latency_s": latency,
            "returned_model": "osnet_x0_25_msmt17",
            "nearest_label": ranked[0][0],
            "best_cosine": ranked[0][1],
            "runner_up_cosine": ranked[1][1],
            "margin": ranked[0][1] - ranked[1][1],
            "scores": dict(ranked),
            "match_state": "probable_outfit" if label else "unmatched",
        }
        with (folder / "predictions.jsonl").open("a") as output:
            output.write(json.dumps(result) + "\n")
        print(f"OSNet {i + 1}/80 {case['id']}: {result['label']}", flush=True)
    benchmark.summarize(folder)


def run_astra():
    # The shared API harness uses data URLs. Crop PNGs must carry the proper MIME
    # type: override image encoding with lossless PNG->JPEG is NOT acceptable for
    # equal pixels, so use a small adapter to the provider's input schema instead.
    benchmark.HERE = HERE
    original_call = benchmark.Provider.call

    def call_with_png(self, items):
        # The API accepts PNG data even when the data URL MIME is image/jpeg on
        # some servers, but this benchmark labels it correctly and records it.
        # Use the same request implementation with content represented below.
        import base64

        if self.name != "astra":
            raise ValueError("This comparison adapter is Astra-only")
        content = []
        for kind, value in items:
            if kind == "text":
                content.append({"type": "input_text", "text": value})
            else:
                prefix = base64.b64decode(value[:32])
                mime = "image/png" if prefix.startswith(b"\x89PNG") else "image/jpeg"
                content.append({"type": "input_image", "image_url": f"data:{mime};base64,{value}", "detail": "high"})
        body = {
            "model": self.model,
            "store": False,
            "reasoning": {"effort": self.effort},
            "max_output_tokens": 2048,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "outfit_prediction",
                    "strict": True,
                    "schema": benchmark.SCHEMA,
                }
            },
        }
        response = self.client.post("https://api.openai.com/v1/responses", json=body)
        response.raise_for_status()
        data = response.json()
        raw = "".join(
            part.get("text", "")
            for item in data.get("output", [])
            for part in item.get("content", [])
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

    benchmark.Provider.call = call_with_png
    try:
        benchmark.run(
            argparse.Namespace(
                provider="astra", model="gpt-6-astra", effort="low", run_name="astra-crops-low", limit=None
            )
        )
    finally:
        benchmark.Provider.call = original_call


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "osnet", "astra"])
    command = parser.parse_args().command
    {"prepare": prepare, "osnet": run_osnet, "astra": run_astra}[command]()
