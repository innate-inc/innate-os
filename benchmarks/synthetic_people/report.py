#!/usr/bin/env python3
"""Build results from complete saved predictions; never call evaluated models."""

from __future__ import annotations

import base64
import csv
import html
import json
from collections import Counter

import benchmark
from benchmark import HERE, core

RUNS = {
    "Astra 6": "astra-low",
    "Gemini 3.6 Flash (Innate proxy)": "gemini-low",
    "PR #807 isolated face path": "pr807-face",
}
FACE = ["face_64", "face_32", "face_16"]
SCENE = ["near_320", "near_160", "far_320", "far_160"]
LABELS = {
    "face_64": "Face crop 64×64",
    "face_32": "Face crop 32×32",
    "face_16": "Face crop 16×16",
    "near_320": "Near scene 320×240",
    "near_160": "Near scene 160×120",
    "far_320": "Distant scene 320×240",
    "far_160": "Distant scene 160×120",
}


def read_run(name, manifest):
    folder = HERE / "results" / name
    rows = [json.loads(line) for line in (folder / "predictions.jsonl").read_text().splitlines()]
    intended = [c for c in manifest["cases"] if name != "pr807-hog-face" or not c["condition"].startswith("face_")]
    by_id = {c["id"]: c for c in intended}
    assert len(rows) == len(by_id) == len({r["id"] for r in rows}), name
    assert {r["id"] for r in rows} == set(by_id)
    for r in rows:
        assert r["expected"] == by_id[r["id"]]["expected"]
        assert r["sha256"] == by_id[r["id"]]["sha256"]
    cfg = json.loads((folder / "config.json").read_text())
    assert cfg["manifest_sha256"] == core.digest(HERE / "manifest.json")
    with (folder / "predictions.csv").open("w", newline="") as output:
        fields = [
            "id",
            "condition",
            "character_index",
            "expected",
            "label",
            "confidence",
            "status",
            "latency_s",
            "returned_model",
            "reason",
            "best_label",
            "best_score",
            "margin",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return rows


def table(data, conditions):
    lines = [
        "| Condition | Method | Known correct | Unknown rejected | Unknown falsely named | Abstained |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for condition in conditions:
        for title, rows in data.items():
            m = core.metrics([r for r in rows if r["condition"] == condition])
            lines.append(
                f"| {LABELS[condition]} | {title} | {m['known_correct']}/{m['known_n']} | {m['unknown_correct']}/{m['unknown_n']} | {m['unknown_false_accepts']}/{m['unknown_n']} | {m['abstentions']}/{m['n']} |"
            )
    return lines


def image_data(path):
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def review(manifest, data, hog):
    esc = html.escape
    gallery = {g["label"]: g for g in manifest["gallery"]}
    indexed = {name: {r["id"]: r for r in rows} for name, rows in {**data, "PR with HOG": hog}.items()}
    parts = [
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Synthetic character benchmark: inspect cases</title><style>body{font:16px system-ui;max-width:1150px;margin:40px auto;padding:0 20px;color:#19212c;background:#f6f8fa}h1{font-size:30px}.gallery{display:flex;flex-wrap:wrap;gap:12px}figure{margin:0;padding:12px;background:white;border:1px solid #dbe2ea;border-radius:8px}figcaption{font-size:13px;margin-top:6px}details{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:12px;margin:10px 0}summary{cursor:pointer}table{border-collapse:collapse;margin:16px 0}td,th{padding:8px;text-align:left;border-bottom:1px solid #ddd}.query{image-rendering:pixelated;max-width:100%;width:auto;height:240px}.muted{color:#536173}</style>",
        "<h1>Synthetic character recognition: inspect every case</h1><p>Entirely fictional, AI-generated characters. Fifteen references; five held-out characters. No real-person photographs. This page includes answers for review; it was never given to evaluated models.</p>",
        '<p class="muted">Query images below are enlarged for inspection. Original input dimensions appear in each case. Face crops contain limited hair and face outline, but no clothing. The lookalike holdouts may contain generator identity ambiguity. Scores are uncalibrated.</p><h2>Model-visible gallery</h2><div class="gallery">',
    ]
    for g in manifest["gallery"]:
        parts.append(
            f'<figure><img width="96" height="96" src="{image_data(HERE / g["path"])}"><figcaption>{esc(g["label"])}</figcaption></figure>'
        )
    parts.append("</div><h2>Queries</h2>")
    for c in sorted(manifest["cases"], key=lambda c: (FACE + SCENE).index(c["condition"]) * 100 + c["character_index"]):
        parts.append(
            f"<details><summary><b>{esc(LABELS[c['condition']])}</b> — character {c['character_index']:02} — expected {esc(c['expected'])}</summary>"
        )
        parts.append(
            f'<p>Actual model input: {c["image_size"][0]}×{c["image_size"][1]} pixels.</p><div class="gallery"><figure><img class="query" src="{image_data(HERE / c["path"])}"><figcaption>QUERY (enlarged, no extra detail)</figcaption></figure>'
        )
        if c["expected"] in gallery:
            g = gallery[c["expected"]]
            parts.append(
                f'<figure><img width="128" height="128" src="{image_data(HERE / g["path"])}"><figcaption>Expected: {esc(c["expected"])}</figcaption></figure>'
            )
        parts.append(
            "</div><table><tr><th>Method</th><th>Returned label</th><th>Confidence / cosine</th><th>Reason</th></tr>"
        )
        for title, by_id in indexed.items():
            if c["id"] not in by_id:
                continue
            r = by_id[c["id"]]
            score = r.get("confidence") if r.get("confidence") is not None else r.get("best_score")
            parts.append(
                f"<tr><td>{esc(title)}</td><td>{esc(str(r['label']))}</td><td>{'—' if score is None else f'{score:.3f}'}</td><td>{esc(r.get('reason', r['status']))}</td></tr>"
            )
        parts.append("</table></details>")
    parts.append("</html>")
    (HERE / "review.html").write_text("\n".join(parts))


def main():
    manifest = benchmark.setup()
    benchmark.validate_inputs(manifest)
    data = {title: read_run(name, manifest) for title, name in RUNS.items()}
    hog = read_run("pr807-hog-face", manifest)
    lines = [
        "# Synthetic face and character recognition — completed results",
        "",
        "Face-based matching **was tested**, using entirely fictional, generated characters and invented names. These results replace no prior clothing scores: this is a separate dataset and task. They do not establish accuracy on real people or robot deployments.",
        "",
        "The test has 15 named reference faces and five held-out fictional characters. Each character appears in seven conditions, for 140 queries per main method. The same references, query bytes and shuffled gallery orders were used for Astra and Gemini. PR #807 received those same images with one stored SFace vector per reference. Clothing is shared among characters and changes between enrollment and query.",
        "",
        "## Face-only results",
        "",
        "The crop dimensions below are **not physical distance or detected face height**. Independently rendered low-angle query faces were cropped at native resolution (154–201 px) and downsampled to 64, 32 and 16 px, JPEG quality 70. Reference crops are 128×128, quality 85. Crops include facial hair and some face outline/hair, but no clothing.",
        "",
    ]
    lines += table(data, FACE)
    lines += [
        "",
        "## Robot-style scene results",
        "",
        "Near/far scenes use generated low camera perspectives, inspired by the supplied robot video. No video pixels were used. Physical distances and optics are illustrative. Projecting source-resolution face boxes through the resize gives approximate face heights: near 320 **21.5–26.9 px**, near 160 **10.8–13.5 px**, far 320 **10.4–14.9 px**, far 160 **5.2–7.5 px**. These are localization-based estimates, not manual ground truth. Full scenes retain head outline and body cues; interpret these separately from the face-only test.",
        "",
    ]
    lines += table(data, SCENE)
    lines += [
        "",
        "## PR #807 with person detection included",
        "",
        "The main PR rows above bypass the person detector and run YuNet over the supplied query, then SFace. The following separate diagnostic uses the PR’s HOG person detector and searches the top 40% of its boxes. It includes no outfit fallback, temporal enrollment, roster updates or robot integration.",
        "",
    ]
    lines += table({"PR HOG → YuNet → SFace": hog}, SCENE)
    reasons = Counter(r["reason"] for r in hog)
    lines += [
        "",
        f"HOG-plus-face outcomes: {dict(reasons)}. No enrolled character was correctly recognized in this scene diagnostic.",
        "",
        "## Timing and validity",
        "",
        "| Method | Completed cases | Errors | Median latency | p95 latency |",
        "|---|---:|---:|---:|---:|",
    ]
    for title, rows in {**data, "PR with HOG (scene cases only)": hog}.items():
        m = core.metrics(rows)
        lines.append(
            f"| {title} | {m['n']} | {m['errors']} | {m['median_latency_s'] * 1000:.1f} ms | {m['p95_latency_s'] * 1000:.1f} ms |"
        )
    lines += [
        "",
        "LLM timings measure API round trips, including network and provider processing. Local timings measure image decode, detection, alignment, embedding and matching as applicable, with models and gallery already loaded, on this Mac CPU. The runner requested one OpenCV thread, but the saved runtime configuration reported 10 threads; single-thread timing is not established. These are different execution environments. The PR’s fast aggregate median includes many early rejections before SFace executes.",
        "",
    ]
    face_rows = data["PR #807 isolated face path"]
    embedded = [r for r in face_rows if r.get("embedded_face_count") == 1]
    m = core.metrics(embedded)
    lines.append(
        f"For the {len(embedded)} PR queries that actually produced a face embedding, median local latency was {m['median_latency_s'] * 1000:.1f} ms."
    )
    for title in list(RUNS)[:2]:
        rows = data[title]
        returned = sorted({r.get("returned_model") for r in rows if r.get("returned_model")})
        lines.append(
            f"- {title}: returned model identifiers `{returned}`. Raw provider token usage is saved per request; proxy dollar pricing is not inferred."
        )
    lines += [
        "",
        "## Interpretation and limitations",
        "",
        "- The face-only tests directly exercise synthetic facial appearance matching across new views. The scene tests also measure whether the model can find and use a tiny face. They are not body-only or clothing-only recognition tests.",
        "- Four of the five held-out designs deliberately resemble gallery characters in broad appearance. Inspection also reveals possible generator convergence between some pairs. Their intended labels come from the generation specification, not verified distinct real identities. False matches therefore combine system confusion with synthetic identity ambiguity. Do not use these counts as real-world false-acceptance estimates.",
        "- Only 20 unique fictional characters and five unique unknowns are present; resolution variants and views are correlated. The condition weights in the overall JSON summary are arbitrary. No significance claim or rare-error guarantee is warranted.",
        "- Full-scene views and enrollment portraits were created within the same source-sheet generation. The separate face-only query sheet was generated using those sheets as references. This can exaggerate visual consistency. Conversely, generator drift can create label noise.",
        "- PR thresholds were unchanged: face cosine ≥0.42, margin ≥0.05, YuNet score ≥0.6 and detected face height ≥24 px. No threshold was tuned on test cases. Missing faces are benchmark abstentions; a valid face embedding that fails matching is UNKNOWN. This distinction prevents detection failure from counting as successful unknown rejection.",
        "- Primary PR results use OpenCV 4.12.0.88 / NumPy 2.2.6. An exploratory OpenCV 5 run is retained but excluded: HOG was unavailable in that build, and small-face localization differed. This runtime sensitivity matters when comparing these local numbers with a Jetson installation.",
        "- No real-person facial identification, automatic enrollment, temporal tracking, crowded scenes, severe occlusion, calibrated motion blur or physical robot execution was tested. A potential real-world system remains unvalidated by this experiment.",
        "",
        "## Charts",
        "",
        "![Correct recognition by image detail](figures/recognition-by-detail.png)",
        "",
        "![Unknown-character outcomes](figures/unknown-outcomes.png)",
        "",
        "[Diagram and vector exports](figures/README.md).",
        "",
        "## Artifacts",
        "",
        "- [Inspect all images and predictions](review.html) — self-contained visual report.",
        "- [Reference/query face QA sheet](assets/qa_faces.jpg).",
        "- [Protocol and reproduction](README.md), [manifest](manifest.json), [crop annotations](annotations.json), [generation provenance](generation_provenance.json).",
        "- [Astra predictions](results/astra-low/predictions.csv), [Gemini predictions](results/gemini-low/predictions.csv), [PR face predictions](results/pr807-face/predictions.csv), [PR with HOG](results/pr807-hog-face/predictions.csv).",
        "- [PR #807 source](https://github.com/innate-inc/innate-os/pull/807), pinned to `487978f9fc69bda93b349423076c3455248ae1c2`; source/weight hashes in `pr807/`.",
        "",
    ]
    (HERE / "RESULTS.md").write_text("\n".join(lines))
    review(manifest, data, hog)
    print("Built RESULTS.md and self-contained review.html from 500 primary saved predictions (280 API, 220 local).")


if __name__ == "__main__":
    main()
