# Synthetic outfit-matching pilot

This evaluates **clothing ensembles**, not personal identity. There are no named
people, faces, body-shape matching, or claims about recognizing someone after a
clothing change. Identical blank retail mannequins wear the outfits.

The original request concerned recognizing 15 people. This pilot covers only
the clothing-matching component; its results cannot answer the facial recognition
question or establish the reliability of a person-recognition system.

## Protocol

- 15 labeled reference outfits, all present in every request as separate images.
- 20 query outfits: those 15 and 5 deliberately similar unknown ensembles.
- Independently generated low-angle and distant scene sheets, with clothing
  references supplied to Imagegen. These are new synthesized views, not independent
  real-world photographs or independently sampled outfits.
- Two sizes per view: 320×240 and 160×120 JPEG, quality 70. Reference crops retain
  their aspect ratio and fit within 320×240, JPEG quality 85.
- 80 queries per model: 60 known, 20 unknown. Only **20 unique outfits**, including
  **5 unique unknowns**; the four conditions repeat them.
- One query per request, no conversational history, no tools, no answer feedback.
- Fixed seed; query order and gallery order shuffled. Both providers receive the
  same image bytes and matching task instructions. Query filenames and truth
  labels never enter the model request.
- Outputs are `OUTFIT_01`…`OUTFIT_15`, `UNKNOWN`, or `UNCERTAIN`, with confidence.
  Confidence is recorded but not treated as calibrated or tuned on this test set.
- `UNKNOWN` means the visible clothing differs from the gallery. `UNCERTAIN` is
  abstention, scored separately. API or JSON errors are failures, not rejections.
- Requested models: `gpt-6-astra` directly through OpenAI Responses, and
  `gemini-3.6-flash` via Innate's native Gemini proxy route. Both use low reasoning
  effort, 2,048 output-token limits, and structured JSON. Astra has `detail=high`;
  Gemini uses its default image processing. Those settings are not equivalent
  computational budgets.
- Requests are sequential within a provider; the two provider runs can overlap.
  Latency measures end-to-end completion, including network, proxy and first-call
  authentication overhead, excluding local image preparation. It is not pure model
  inference time. There are no automatic benchmark retries; interrupted runs resume.

Unknown false-accept rate is the fraction of unknown queries assigned a known label.
Known accuracy must be considered alongside it: always answering `UNKNOWN` gets
perfect unknown rejection but zero known accuracy. Overall accuracy counts errors
and abstentions as incorrect. Balanced accuracy averages known and unknown accuracy.

## Relationship to the robot video

The supplied [robot recording folder](https://drive.google.com/drive/folders/1UuqbO8thaf0GA8Bzpm1Nys9h0YdJV75C)
was visually inspected in Drive (`main.mp4`, approximately 6:24, including early
hallway frames and a view around 1:33). It shows side-by-side stereo views, low
floor-level framing, office furniture, variable indoor lighting and image softness.
The generated queries approximate that appearance and use a single camera view.
No source-video pixels were sent to the benchmark providers or used as scored data.

Repository calibration context comes from `innate/geometry.py`: 640×480 image
coordinates, `HEAD_ORIGIN.z=0.25882`, `FX=200.3`, `FY=267.3`. The origin is relative
to `base_link`; **26 cm is an approximate viewpoint cue, not a calibrated camera
height above the actual floor**. Imagegen did not render calibrated pinhole geometry.
Distance, pitch, lens distortion, blur, and lighting have no measured physical match.

## Reproduction

From the repository root, create a virtual environment and install
`benchmarks/outfit_matching/requirements.txt`. The harness imports the repository's
`innate_proxy` and `auth_client` packages directly and loads the root `.env`.
It never prints credentials. Set `OPENAI_API_KEY` and `INNATE_SERVICE_KEY`; proxy
and OIDC URLs use the same defaults as the robot unless overridden by environment.

```sh
python benchmarks/outfit_matching/benchmark.py prepare
python benchmarks/outfit_matching/benchmark.py run --provider astra --model gpt-6-astra --run-name astra-low
python benchmarks/outfit_matching/benchmark.py run --provider gemini --model gemini-3.6-flash --run-name gemini-low
```

These are live, billed model calls. Reuse a run name to resume only if its model,
settings, harness, prompt and manifest hashes are unchanged. Already recorded errors
are preserved; use a new run name for a new experiment. `--limit 1` is a smoke test;
rerun without the limit to complete that same experiment.

`panel_layouts.json` records visually inspected crop boundaries. `manifest.json`
records all input hashes, reference order, queries, labels, and the fixed shuffle.
Each result directory contains configuration, raw response text, returned model IDs,
token usage, per-query timings, predictions as JSONL/CSV, and summary counts. Secrets,
request headers, and hidden reasoning are not saved.

## Assets and limitations

The built-in Imagegen tool generated the source sheets. Exact prompts are in
`prompts/reference.txt`, `prompts/low_angle.txt`, `prompts/distant.txt`, and
`prompts/distant_correction.txt`. The first distant draft was corrected before
scoring because its subjects were too large; [QA.md](QA.md) records the inspection.
Tool outputs are saved under `assets/`; original generation resolutions are retained. Only panel
extraction, requested downsampling, and JPEG encoding are performed in Python.
Image generation is stochastic; saved assets and hashes reproduce the evaluation,
not identical regeneration from prompts.

This is a small synthetic feasibility check. Mannequins, conspicuous clothing colors,
shared generation references and repeated outfits can make it easier than deployment.
Garment drift between generations can introduce label ambiguity; source sheets must
be reviewed before scoring. No crowded scenes, natural walking dynamics, severe
occlusion, real camera noise calibration, or clothing changes are represented.

The native low-angle panels are approximately 320×240; a 640×480 export would mostly
upscale them. The experiment therefore uses actual smaller inputs and makes no claim
to validate native 640×480 camera performance. The 160×120 variant reduces the full
frame, preserving the subject's fraction of it.

Five unknown outfits per condition are too few to establish a low operational false
accept rate. For example, even 0/5 errors leaves a one-sided 95% binomial upper bound
of about 45%, assuming independent unknown samples. Repeating the same five outfits
does not provide twenty independent unknown identities. No aggregate confidence
interval that treats those repeats as independent should be reported.

API references: [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra),
[OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision),
[Gemini thinking settings](https://ai.google.dev/gemini-api/docs/generate-content/thinking).
