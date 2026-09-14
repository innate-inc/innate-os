#!/usr/bin/env python3
"""Create a report for the fixed synthetic garment comparison, from saved data."""

import json
from datetime import datetime, timezone
from pathlib import Path

from benchmark import metrics

ROOT = Path(__file__).resolve().parent
HERE = ROOT / "pr807"


def fraction(n, total):
    return f"{n}/{total} ({100 * n / total:.1f}%)" if total else "n/a"


def read_run(folder, expected_ids):
    rows = [json.loads(line) for line in (folder / "predictions.jsonl").read_text().splitlines()]
    if len(rows) != len(expected_ids) or {r["id"] for r in rows} != expected_ids:
        raise ValueError(f"Incomplete or duplicate results in {folder}")
    return rows, json.loads((folder / "config.json").read_text())


def main():
    manifest = json.loads((HERE / "manifest.json").read_text())
    ids = {c["id"] for c in manifest["cases"]}
    osnet, osnet_config = read_run(HERE / "results" / "osnet-pr807", ids)
    astra, astra_config = read_run(HERE / "results" / "astra-crops-low", ids)
    original, _ = read_run(ROOT / "results" / "astra-low", ids)
    lines = [
        "# Astra versus PR #807's isolated outfit matcher",
        "",
        f"Completed report: {datetime.now(timezone.utc).isoformat()}.",
        "",
        "**Clothing matching on identical faceless synthetic mannequins only. "
        "This is not a facial recognition or personal identity benchmark, and does not validate the full PR.**",
        "",
        "PR commit: `487978f9fc69bda93b349423076c3455248ae1c2`. "
        "15 reference outfits, 5 unknown lookalikes, 80 queries per method. "
        "Both methods receive identical manually cropped query images and unchanged catalog references. "
        "OSNet uses the PR's exact encoder and matching rule with its original 0.78 accept threshold "
        "and 0.07 margin. There is no threshold tuning. [PR #807](https://github.com/innate-inc/innate-os/pull/807).",
        "",
        "| Method / input | Overall correct | Known outfit correct | Unknown falsely matched | Known outfit rejected | Median / p95 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, rows in [
        ("Astra 6 — same manual crops", astra),
        ("PR OSNet + original decision rule — same manual crops", osnet),
        ("Earlier Astra 6 — whole frames (context only)", original),
    ]:
        m = metrics(rows)
        rejected = sum(r["expected"] != "UNKNOWN" and r["label"] in ("UNKNOWN", "UNCERTAIN") for r in rows)
        timing = f"{m['median_latency_s'] * 1000:.1f} / {m['p95_latency_s'] * 1000:.1f} ms"
        lines.append(
            f"| {label} | {fraction(m['correct'], m['n'])} | "
            f"{fraction(m['known_correct'], m['known_n'])} | "
            f"{fraction(m['unknown_false_accepts'], m['unknown_n'])} | "
            f"{fraction(rejected, m['known_n'])} | {timing} |"
        )
    lines.extend(
        [
            "",
            "Astra abstained on one unknown query; abstentions count as incorrect overall but are not false matches. "
            "Both runs had zero API, encoder, or formatting errors. "
            "OSNet runs on this Mac's CPU; Astra timing includes API and network latency. "
            "No Jetson timing or detector latency is measured. A rejected known outfit is a miss, "
            "so zero unknown false matches must not be read as perfect recognition. "
            "The earlier whole-frame Astra result is a separate single run: differences may reflect both "
            "preprocessing and model variability, so they do not establish a causal cropping effect.",
            "",
            "## Per-condition results",
            "",
            "The condition names refer to the source frame size before cropping. `160` means "
            "160×120 source frames; `320` means 320×240. The distant crops themselves are "
            "44×62 and 88×123 pixels, with no artificial detail added.",
            "",
            "| Condition | Method | Known correct | Unknown falsely matched | Overall correct |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for condition in ("low_angle_320", "low_angle_160", "distant_320", "distant_160"):
        for label, rows in [("Astra crops", astra), ("PR outfit matcher", osnet)]:
            m = metrics([r for r in rows if r["condition"] == condition])
            lines.append(
                f"| {condition} | {label} | {fraction(m['known_correct'], m['known_n'])} | "
                f"{fraction(m['unknown_false_accepts'], m['unknown_n'])} | {fraction(m['correct'], m['n'])} |"
            )
    lines.extend(
        [
            "",
            "## Why OSNet rejected distant outfits",
            "",
            "These diagnostics use the already-saved scores; they do not change the decision rule or rerun the test.",
            "",
            "| Condition | Correct nearest label among known outfits, ignoring rejection | Known below 0.78 | Best-score range for known outfits |",
            "|---|---:|---:|---:|",
        ]
    )
    for condition in ("low_angle_320", "low_angle_160", "distant_320", "distant_160"):
        known = [r for r in osnet if r["condition"] == condition and r["expected"] != "UNKNOWN"]
        lines.append(
            f"| {condition} | {sum(r['nearest_label'] == r['expected'] for r in known)}/{len(known)} | "
            f"{sum(r['best_cosine'] < 0.78 for r in known)}/{len(known)} | "
            f"{min(r['best_cosine'] for r in known):.3f}–{max(r['best_cosine'] for r in known):.3f} |"
        )
    lines.extend(
        [
            "",
            "A nearest-label score is only a diagnostic; the PR does not emit those labels when its gates reject. "
            "OSNet's result can be influenced by low clothing detail, the synthetic domain, and the "
            "background retained by this manual crop policy. It cannot be attributed solely to camera distance. "
            "The supplied regions are not exact detector boxes; the PR's real detection-and-enrollment path is excluded.",
            "",
            "## Astra crop failures",
            "",
        ]
    )
    failures = [r for r in astra if r["label"] != r["expected"]]
    if not failures:
        lines.append("No incorrect decisions on this cropped set.")
    for r in failures:
        lines.append(
            f"- [{r['id']}](pr807/{r['path']}): expected `{r['expected']}`, got `{r['label']}`, "
            f"reported confidence {r['confidence']}."
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This separates two behaviors: retrieving a known outfit and refusing an unfamiliar one. "
            "The PR's outfit rule is conservative on these distant synthetic crops, which avoids false matches "
            "while also losing known outfits. Astra can produce a confident label even when detail is weak. "
            "A confidence number or zero false matches alone is insufficient evidence of reliable operation.",
            "",
            "The existing mannequins and fixed manual regions do not provide a fair assessment of the full "
            "people-recognition PR. They provide a reproducible component comparison under a declared input policy. "
            "Only five unique unknown outfits are repeated across conditions; no rare-error guarantee or "
            "general model ranking follows. Real clothing footage would be needed to test the garment component "
            "under actual camera, background, pose and detector variation.",
            "",
            "## Reproduction and audit trail",
            "",
            f"- OSNet initialization plus 15 reference encodings: {osnet_config['model_load_and_gallery_s']:.3f} s; "
            "three extra warmup calls excluded from per-query timing.",
            f"- Local host: `{osnet_config['host']}` / `{osnet_config['machine']}`; CPUExecutionProvider.",
            f"- Astra requested `{astra_config['model']}`, reasoning `{astra_config['effort']}`; "
            "proper PNG MIME types were used for crops. The previous whole-frame run is unchanged.",
            "- Both cropped experiments completed 80 distinct queries. Per-query data retains all rejections "
            "and failures. OSNet cosine similarity is not interpreted as a probability.",
            "- [Protocol and commands](pr807/README.md), [crop manifest](pr807/manifest.json), "
            "[pinned source metadata](pr807/source.json).",
            "- [OSNet CSV](pr807/results/osnet-pr807/predictions.csv), "
            "[OSNet scores and decisions](pr807/results/osnet-pr807/predictions.jsonl), "
            "[OSNet runtime configuration](pr807/results/osnet-pr807/config.json).",
            "- [Astra cropped CSV](pr807/results/astra-crops-low/predictions.csv), "
            "[Astra raw output and usage](pr807/results/astra-crops-low/predictions.jsonl).",
            "- [Earlier full-frame Astra and Gemini report](RESULTS.md).",
            "",
        ]
    )
    path = ROOT / "PR807_COMPARISON.md"
    path.write_text("\n".join(lines))
    print(path)


if __name__ == "__main__":
    main()
