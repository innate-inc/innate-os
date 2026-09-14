# PR #807: isolated clothing-matcher comparison

This experiment compares Astra with **only the outfit encoder and outfit decision
rule** from [PR #807](https://github.com/innate-inc/innate-os/pull/807), pinned to
commit `487978f9fc69bda93b349423076c3455248ae1c2`. Subjects are the existing identical
faceless synthetic mannequins. Labels identify clothing ensembles, not people.

**This is not an evaluation of the full PR.** Person detection, face detection,
facial identification, person enrollment, gallery updates, expiry, and tracking
are excluded. In the actual PR, an outfit match supplies a tentative `probable`
association after facial enrollment. Here, clothing reference vectors are supplied
directly as the experimental gallery; a match is scored only as an outfit match.

## What is held fixed

- Same 15 catalog references and 80 underlying query images as the earlier pilot.
- Same randomized query order and reference order, same garment labels and truth.
- Both methods receive the exact same reference files and cropped query pixels.
- References are copied byte-for-byte. Query crops are saved losslessly as PNG;
  no detail is invented, sharpened, or regenerated.
- Crop rectangles are selected manually before either cropped-model run, with
  one fixed rectangle per scene and proportional scaling for smaller frames.
  Coordinates are in `manifest.json` and `../pr807_compare.py`. No crop or threshold
  is optimized against the scored answers.
- Astra uses the previous clothing-only prompt, all 15 images in context,
  `gpt-6-astra`, low reasoning, image detail high, and a 2,048-output-token limit.
- OSNet uses the PR's bundled `osnet_x0_25_msmt17.onnx`, exact preprocessing,
  normalized embeddings and exact `_best` function. Accept threshold **0.78** and
  runner-up margin **0.07** remain unchanged. No calibration is performed.
- The extracted encoder and matching function were compared structurally against
  the pinned PR source using Python ASTs. Model SHA-256 is verified before running.

The 320×240 near frames produce 136×238 crops, and the 160×120 near frames produce
68×119 crops. The distant crops are 88×123 and 44×62 pixels, respectively. The
mannequin occupies only part of each crop. These are approximate manual subject
regions, **not detector-produced tight boxes**. Background occupancy differs
between catalog, near, and distant crops and may affect OSNet. Results apply to
this declared crop policy, not an ideal detector or a calibrated robot pipeline.

## Timing

OSNet runs locally on macOS ARM64 through ONNX Runtime's CPU provider. Model loading
and the 15-reference gallery initialization are reported separately; three warmup
calls precede the sequential measured run. Query timings include image decoding,
preprocessing, embedding, and comparison with the 15 reference vectors.

Astra timing is end-to-end API completion through the network, with local base64
preparation excluded. These are different architectures and hardware. Local OSNet
timings are **not Jetson measurements**; both methods exclude the manual crop-making
step and any real detector latency. Dependencies and host are saved in each run's
configuration. No identities or robot state are written.

## Reproduce

Install `../requirements.txt` and this directory's `requirements.txt` in a virtual
environment. From the repository root:

```sh
python benchmarks/outfit_matching/pr807_compare.py prepare
python benchmarks/outfit_matching/pr807_compare.py osnet
python benchmarks/outfit_matching/pr807_compare.py astra
python benchmarks/outfit_matching/pr807_report.py
```

The Astra command makes billed API calls using the root `.env` and resumes its
unchanged experiment if interrupted. The OSNet command refuses to overwrite an
existing result. Preserve existing result directories before starting a new run.
Preparation is deterministic, and image/harness/manifest hashes are saved.

`source.json` records the PR commit, source hashes, model hash and thresholds.
`extracted_outfit.py` contains the unmodified encoder and generic comparison
definitions, with only their required imports. Face identification and enrollment
code were not copied into this executable module. `results/` contains all per-query
labels, nearest-match scores and margins, timing, and summaries. Cosine similarity
is not reported as a probability or compared numerically with Astra confidence.

All caveats from [the original pilot](../README.md) still apply: only 20 unique
synthetic outfits, including five unique unknowns; repeated views and sizes are
correlated. This cannot establish accuracy on real humans or faces. The manually
cropped run is an additional preprocessing experiment on the same images, not an
independent test set.
