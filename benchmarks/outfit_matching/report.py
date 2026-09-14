#!/usr/bin/env python3
"""Build the result report from saved predictions, without making API calls."""

import json
from datetime import datetime, timezone
from pathlib import Path

from benchmark import LABELS, metrics

HERE = Path(__file__).resolve().parent


def fraction(a, b):
    return f"{a}/{b} ({100 * a / b:.1f}%)" if b else "n/a"


def main():
    experiments = []
    expected_ids = {case["id"] for case in json.loads((HERE / "manifest.json").read_text())["cases"]}
    for name in ("astra-low", "gemini-low"):
        folder = HERE / "results" / name
        rows = [json.loads(line) for line in (folder / "predictions.jsonl").read_text().splitlines()]
        if len(rows) != len(expected_ids) or {row["id"] for row in rows} != expected_ids:
            raise ValueError(f"{name} is incomplete or contains duplicate/unexpected cases; finish the run first")
        config = json.loads((folder / "config.json").read_text())
        experiments.append((name, config, rows))
    lines = [
        "# Outfit matching: Astra 6 versus Gemini through Innate",
        "",
        f"Report generated {datetime.now(timezone.utc).isoformat()}.",
        "",
        "**This measures matching synthetic clothing ensembles on blank mannequins. "
        "It does not measure facial recognition, body-based identity, or recognition of named people.**",
        "",
        "15 reference outfits are included in every request. Each model sees the same 80 queries: "
        "15 known and 5 unknown outfits across two viewpoints and two image sizes. "
        "There are 20 unique outfits, including only 5 unique unknowns. "
        "The scene appearance was informed by the supplied robot video; the scored images are generated.",
        "",
        "| Model | Overall accuracy | Known outfit accuracy | Unknown rejection | Unknown false accepts | Abstentions / errors | Median / p95 latency |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, config, rows in experiments:
        m = metrics(rows)
        times = (
            f"{m['median_latency_s']:.2f}s / {m['p95_latency_s']:.2f}s" if m["median_latency_s"] is not None else "n/a"
        )
        lines.append(
            f"| {config['model']} | {fraction(m['correct'], m['n'])} | "
            f"{fraction(m['known_correct'], m['known_n'])} | "
            f"{fraction(m['unknown_correct'], m['unknown_n'])} | "
            f"{fraction(m['unknown_false_accepts'], m['unknown_n'])} | "
            f"{m['abstentions']} / {m['errors']} | {times} |"
        )
    lines.extend(
        [
            "",
            "The small distant views expose the main weakness: matching a known outfit can remain "
            "accurate while rejecting an unknown lookalike fails. The counts below separate those tasks.",
            "",
        ]
    )
    for _name, config, rows in experiments:
        small = metrics([r for r in rows if r["condition"] == "distant_160"])
        near = metrics([r for r in rows if r["condition"].startswith("low_angle_")])
        wrong = [r["confidence"] for r in rows if r["status"] == "ok" and r["label"] != r["expected"]]
        confidence_note = f" Wrong answers had reported confidence {min(wrong):.2f}–{max(wrong):.2f}." if wrong else ""
        lines.append(
            f"- **{config['model']}**: nearby views {fraction(near['correct'], near['n'])}; "
            f"small distant unknowns falsely matched {small['unknown_false_accepts']}/{small['unknown_n']}."
            + confidence_note
        )
    lines.extend(
        [
            "",
            "The model's self-reported confidence is therefore insufficient by itself to prevent false matches "
            "in this sample. A larger test on real robot outfit footage would be needed before choosing a system. "
            "These results cannot establish personal identity recognition or performance after clothing changes.",
            "",
            "## Conditions",
            "",
            "| Condition | Model | Known accuracy | Unknown rejection | Unknown false accepts | Balanced accuracy |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for condition in ("low_angle_320", "low_angle_160", "distant_320", "distant_160"):
        for _, config, rows in experiments:
            m = metrics([r for r in rows if r["condition"] == condition])
            balanced = (
                (m["known_correct"] / m["known_n"] + m["unknown_correct"] / m["unknown_n"]) / 2
                if m["known_n"] and m["unknown_n"]
                else None
            )
            score = f"{100 * balanced:.1f}%" if balanced is not None else "n/a"
            lines.append(
                f"| {condition} | {config['model']} | {fraction(m['known_correct'], m['known_n'])} | "
                f"{fraction(m['unknown_correct'], m['unknown_n'])} | "
                f"{fraction(m['unknown_false_accepts'], m['unknown_n'])} | {score} |"
            )
    lines.extend(
        [
            "",
            "`320` means 320×240 pixels; `160` means 160×120 pixels. "
            "Both views are from a low camera. The distant scene has a smaller subject in the frame. "
            "Viewpoint and distance are synthesized, not metrically calibrated.",
            "",
            "`UNKNOWN` is explicit rejection; `UNCERTAIN` is abstention. "
            "Errors and abstentions count as incorrect. An always-unknown classifier would get "
            "25% overall accuracy and 50% balanced accuracy. Uniform guessing among the 15 "
            "reference labels gives 6.7% known-outfit accuracy.",
            "",
            "## What failed",
            "",
        ]
    )
    for _name, config, rows in experiments:
        failures = [r for r in rows if r["label"] != r["expected"]]
        lines.append(f"**{config['model']}**: {len(failures)} incorrect decisions or errors.")
        lines.append("")
        for r in failures:
            kind = (
                "false accept of unknown"
                if r["expected"] == "UNKNOWN" and r["label"] in LABELS
                else "incorrect / abstention / error"
            )
            lines.append(
                f"- [{r['id']}]({r['path']}): expected `{r['expected']}`, got `{r['label']}` "
                f"(confidence {r.get('confidence')}; {kind})."
            )
        lines.append("")
    lines.extend(
        [
            "## Example inputs",
            "",
            "Known outfit 1: catalog, low view, and distant view. The distant query below is 160×120.",
            "",
            "![Reference outfit](assets/gallery/OUTFIT_01.jpg)",
            "",
            "![Low-angle query](assets/queries/low_angle_320_01.jpg)",
            "",
            "![Distant small query](assets/queries/distant_160_01.jpg)",
            "",
            "Unknown outfit 16 resembles outfit 1 but has black shorts and black shoes:",
            "",
            "![Unknown outfit query](assets/queries/distant_160_16.jpg)",
            "",
            "## API settings, usage, and reproducibility",
            "",
        ]
    )
    for name, config, rows in experiments:
        returned = sorted({r.get("returned_model") for r in rows if r.get("returned_model")})
        lines.append(
            f"- **{name}**: requested `{config['model']}`; returned {', '.join(returned) or 'unavailable'}; "
            f"reasoning `{config['effort']}`; {len(rows)} saved requests. "
            f"[Configuration](results/{name}/config.json), [CSV](results/{name}/predictions.csv), "
            f"[Raw output and usage](results/{name}/predictions.jsonl)."
        )
        if config["provider"] == "astra":
            input_tokens = sum(r.get("usage", {}).get("input_tokens", 0) for r in rows)
            cached = sum(r.get("usage", {}).get("input_tokens_details", {}).get("cached_tokens", 0) for r in rows)
            writes = sum(r.get("usage", {}).get("input_tokens_details", {}).get("cache_write_tokens", 0) for r in rows)
            output_tokens = sum(r.get("usage", {}).get("output_tokens", 0) for r in rows)
            cost = ((input_tokens - cached - writes) * 10 + cached + writes * 12.5 + output_tokens * 50) / 1e6
            lines.append(
                f"  Input tokens {input_tokens:,}; cached reads {cached:,}; cache writes {writes:,}; "
                f"output tokens {output_tokens:,}. Estimated standard API inference cost **${cost:.3f}**, "
                "using [published Astra rates](https://developers.openai.com/api/docs/models/gpt-6-astra). "
                "This excludes image generation, the separate connectivity check, and taxes; it is not an invoice."
            )
        else:
            inp = sum(r.get("usage", {}).get("promptTokenCount", 0) for r in rows)
            out = sum(r.get("usage", {}).get("candidatesTokenCount", 0) for r in rows)
            thought = sum(r.get("usage", {}).get("thoughtsTokenCount", 0) for r in rows)
            lines.append(
                f"  Prompt tokens {inp:,}; output tokens {out:,}; reported thinking tokens {thought:,}. "
                "Innate proxy billing was not available; no dollar estimate is asserted."
            )
    lines.extend(
        [
            "",
            "Latency is wall-clock time to completion, including the network and proxy. "
            "The models run sequentially within each provider, with the two providers overlapping. "
            "Reasoning settings and provider image processing do not imply equal compute. "
            "There are no automatic retries and no threshold tuned on these test outcomes.",
            "",
            "## Limits on conclusions",
            "",
            "The generated outfits and mannequin scenes are a small feasibility pilot, not a deployment "
            "validation set. Strong results can reflect conspicuous garment colors and synthesis consistency. "
            "There are no clothing changes, crowds, severe occlusions, or calibrated real-camera corruption.",
            "",
            "Only five unique unknown outfits were tested. Results pooled over views and sizes are correlated. "
            "Even 0/5 errors in one condition would leave a one-sided 95% binomial upper bound near 45% "
            "under an independent-sample assumption. This pilot cannot establish a rare false-accept rate.",
            "",
            "[Full protocol and reproduction instructions](README.md), "
            "[dataset manifest](manifest.json), [panel QA notes](QA.md).",
            "",
        ]
    )
    (HERE / "RESULTS.md").write_text("\n".join(lines))
    print(HERE / "RESULTS.md")


if __name__ == "__main__":
    main()
