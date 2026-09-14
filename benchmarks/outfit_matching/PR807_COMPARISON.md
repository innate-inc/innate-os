# Astra versus PR #807's isolated outfit matcher

Completed report: 2026-09-09T15:31:40.330323+00:00.

**Clothing matching on identical faceless synthetic mannequins only. This is not a facial recognition or personal identity benchmark, and does not validate the full PR.**

PR commit: `487978f9fc69bda93b349423076c3455248ae1c2`. 15 reference outfits, 5 unknown lookalikes, 80 queries per method. Both methods receive identical manually cropped query images and unchanged catalog references. OSNet uses the PR's exact encoder and matching rule with its original 0.78 accept threshold and 0.07 margin. There is no threshold tuning. [PR #807](https://github.com/innate-inc/innate-os/pull/807).

| Method / input | Overall correct | Known outfit correct | Unknown falsely matched | Known outfit rejected | Median / p95 |
|---|---:|---:|---:|---:|---:|
| Astra 6 — same manual crops | 70/80 (87.5%) | 56/60 (93.3%) | 5/20 (25.0%) | 0/60 (0.0%) | 3132.6 / 5399.1 ms |
| PR OSNet + original decision rule — same manual crops | 50/80 (62.5%) | 30/60 (50.0%) | 0/20 (0.0%) | 30/60 (50.0%) | 12.2 / 42.7 ms |
| Earlier Astra 6 — whole frames (context only) | 74/80 (92.5%) | 59/60 (98.3%) | 5/20 (25.0%) | 0/60 (0.0%) | 3201.7 / 6063.6 ms |

Astra abstained on one unknown query; abstentions count as incorrect overall but are not false matches. Both runs had zero API, encoder, or formatting errors. OSNet runs on this Mac's CPU; Astra timing includes API and network latency. No Jetson timing or detector latency is measured. A rejected known outfit is a miss, so zero unknown false matches must not be read as perfect recognition. The earlier whole-frame Astra result is a separate single run: differences may reflect both preprocessing and model variability, so they do not establish a causal cropping effect.

## Per-condition results

The condition names refer to the source frame size before cropping. `160` means 160×120 source frames; `320` means 320×240. The distant crops themselves are 44×62 and 88×123 pixels, with no artificial detail added.

| Condition | Method | Known correct | Unknown falsely matched | Overall correct |
|---|---|---:|---:|---:|
| low_angle_320 | Astra crops | 15/15 (100.0%) | 0/5 (0.0%) | 20/20 (100.0%) |
| low_angle_320 | PR outfit matcher | 15/15 (100.0%) | 0/5 (0.0%) | 20/20 (100.0%) |
| low_angle_160 | Astra crops | 15/15 (100.0%) | 0/5 (0.0%) | 20/20 (100.0%) |
| low_angle_160 | PR outfit matcher | 15/15 (100.0%) | 0/5 (0.0%) | 20/20 (100.0%) |
| distant_320 | Astra crops | 14/15 (93.3%) | 2/5 (40.0%) | 17/20 (85.0%) |
| distant_320 | PR outfit matcher | 0/15 (0.0%) | 0/5 (0.0%) | 5/20 (25.0%) |
| distant_160 | Astra crops | 12/15 (80.0%) | 3/5 (60.0%) | 13/20 (65.0%) |
| distant_160 | PR outfit matcher | 0/15 (0.0%) | 0/5 (0.0%) | 5/20 (25.0%) |

## Why OSNet rejected distant outfits

These diagnostics use the already-saved scores; they do not change the decision rule or rerun the test.

| Condition | Correct nearest label among known outfits, ignoring rejection | Known below 0.78 | Best-score range for known outfits |
|---|---:|---:|---:|
| low_angle_320 | 15/15 | 0/15 | 0.783–0.927 |
| low_angle_160 | 15/15 | 0/15 | 0.793–0.932 |
| distant_320 | 12/15 | 15/15 | 0.510–0.687 |
| distant_160 | 6/15 | 15/15 | 0.506–0.644 |

A nearest-label score is only a diagnostic; the PR does not emit those labels when its gates reject. OSNet's result can be influenced by low clothing detail, the synthetic domain, and the background retained by this manual crop policy. It cannot be attributed solely to camera distance. The supplied regions are not exact detector boxes; the PR's real detection-and-enrollment path is excluded.

## Astra crop failures

- [distant_160_04](pr807/crops/queries/distant_160_04.png): expected `OUTFIT_04`, got `OUTFIT_12`, reported confidence 0.92.
- [distant_160_17](pr807/crops/queries/distant_160_17.png): expected `UNKNOWN`, got `OUTFIT_12`, reported confidence 0.94.
- [distant_160_08](pr807/crops/queries/distant_160_08.png): expected `OUTFIT_08`, got `OUTFIT_12`, reported confidence 0.87.
- [distant_160_06](pr807/crops/queries/distant_160_06.png): expected `OUTFIT_06`, got `OUTFIT_12`, reported confidence 0.87.
- [distant_320_20](pr807/crops/queries/distant_320_20.png): expected `UNKNOWN`, got `OUTFIT_08`, reported confidence 0.94.
- [distant_160_20](pr807/crops/queries/distant_160_20.png): expected `UNKNOWN`, got `OUTFIT_12`, reported confidence 0.92.
- [distant_160_18](pr807/crops/queries/distant_160_18.png): expected `UNKNOWN`, got `UNCERTAIN`, reported confidence 0.83.
- [distant_320_04](pr807/crops/queries/distant_320_04.png): expected `OUTFIT_04`, got `OUTFIT_12`, reported confidence 0.9.
- [distant_160_19](pr807/crops/queries/distant_160_19.png): expected `UNKNOWN`, got `OUTFIT_14`, reported confidence 0.78.
- [distant_320_18](pr807/crops/queries/distant_320_18.png): expected `UNKNOWN`, got `OUTFIT_03`, reported confidence 0.91.

## Interpretation

This separates two behaviors: retrieving a known outfit and refusing an unfamiliar one. The PR's outfit rule is conservative on these distant synthetic crops, which avoids false matches while also losing known outfits. Astra can produce a confident label even when detail is weak. A confidence number or zero false matches alone is insufficient evidence of reliable operation.

The existing mannequins and fixed manual regions do not provide a fair assessment of the full people-recognition PR. They provide a reproducible component comparison under a declared input policy. Only five unique unknown outfits are repeated across conditions; no rare-error guarantee or general model ranking follows. Real clothing footage would be needed to test the garment component under actual camera, background, pose and detector variation.

## Reproduction and audit trail

- OSNet initialization plus 15 reference encodings: 11.816 s; three extra warmup calls excluded from per-query timing.
- Local host: `macOS-26.2-arm64-arm-64bit` / `arm64`; CPUExecutionProvider.
- Astra requested `gpt-6-astra`, reasoning `low`; proper PNG MIME types were used for crops. The previous whole-frame run is unchanged.
- Both cropped experiments completed 80 distinct queries. Per-query data retains all rejections and failures. OSNet cosine similarity is not interpreted as a probability.
- [Protocol and commands](pr807/README.md), [crop manifest](pr807/manifest.json), [pinned source metadata](pr807/source.json).
- [OSNet CSV](pr807/results/osnet-pr807/predictions.csv), [OSNet scores and decisions](pr807/results/osnet-pr807/predictions.jsonl), [OSNet runtime configuration](pr807/results/osnet-pr807/config.json).
- [Astra cropped CSV](pr807/results/astra-crops-low/predictions.csv), [Astra raw output and usage](pr807/results/astra-crops-low/predictions.jsonl).
- [Earlier full-frame Astra and Gemini report](RESULTS.md).
