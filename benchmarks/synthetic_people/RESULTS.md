# Synthetic face and character recognition — completed results

Face-based matching **was tested**, using entirely fictional, generated characters and invented names. These results replace no prior clothing scores: this is a separate dataset and task. They do not establish accuracy on real people or robot deployments.

The test has 15 named reference faces and five held-out fictional characters. Each character appears in seven conditions, for 140 queries per main method. The same references, query bytes and shuffled gallery orders were used for Astra and Gemini. PR #807 received those same images with one stored SFace vector per reference. Clothing is shared among characters and changes between enrollment and query.

## Face-only results

The crop dimensions below are **not physical distance or detected face height**. Independently rendered low-angle query faces were cropped at native resolution (154–201 px) and downsampled to 64, 32 and 16 px, JPEG quality 70. Reference crops are 128×128, quality 85. Crops include facial hair and some face outline/hair, but no clothing.

| Condition | Method | Known correct | Unknown rejected | Unknown falsely named | Abstained |
|---|---|---:|---:|---:|---:|
| Face crop 64×64 | Astra 6 | 14/15 | 0/5 | 5/5 | 0/20 |
| Face crop 64×64 | Gemini 3.6 Flash (Innate proxy) | 15/15 | 0/5 | 5/5 | 0/20 |
| Face crop 64×64 | PR #807 isolated face path | 15/15 | 0/5 | 5/5 | 0/20 |
| Face crop 32×32 | Astra 6 | 14/15 | 0/5 | 5/5 | 0/20 |
| Face crop 32×32 | Gemini 3.6 Flash (Innate proxy) | 12/15 | 0/5 | 5/5 | 3/20 |
| Face crop 32×32 | PR #807 isolated face path | 4/15 | 2/5 | 0/5 | 13/20 |
| Face crop 16×16 | Astra 6 | 8/15 | 0/5 | 5/5 | 3/20 |
| Face crop 16×16 | Gemini 3.6 Flash (Innate proxy) | 0/15 | 0/5 | 0/5 | 20/20 |
| Face crop 16×16 | PR #807 isolated face path | 0/15 | 0/5 | 0/5 | 20/20 |

## Robot-style scene results

Near/far scenes use generated low camera perspectives, inspired by the supplied robot video. No video pixels were used. Physical distances and optics are illustrative. Projecting source-resolution face boxes through the resize gives approximate face heights: near 320 **21.5–26.9 px**, near 160 **10.8–13.5 px**, far 320 **10.4–14.9 px**, far 160 **5.2–7.5 px**. These are localization-based estimates, not manual ground truth. Full scenes retain head outline and body cues; interpret these separately from the face-only test.

| Condition | Method | Known correct | Unknown rejected | Unknown falsely named | Abstained |
|---|---|---:|---:|---:|---:|
| Near scene 320×240 | Astra 6 | 13/15 | 0/5 | 5/5 | 0/20 |
| Near scene 320×240 | Gemini 3.6 Flash (Innate proxy) | 15/15 | 0/5 | 5/5 | 0/20 |
| Near scene 320×240 | PR #807 isolated face path | 0/15 | 1/5 | 0/5 | 12/20 |
| Near scene 160×120 | Astra 6 | 8/15 | 0/5 | 4/5 | 2/20 |
| Near scene 160×120 | Gemini 3.6 Flash (Innate proxy) | 13/15 | 0/5 | 5/5 | 0/20 |
| Near scene 160×120 | PR #807 isolated face path | 0/15 | 0/5 | 0/5 | 20/20 |
| Distant scene 320×240 | Astra 6 | 11/15 | 0/5 | 2/5 | 5/20 |
| Distant scene 320×240 | Gemini 3.6 Flash (Innate proxy) | 11/15 | 0/5 | 5/5 | 0/20 |
| Distant scene 320×240 | PR #807 isolated face path | 0/15 | 0/5 | 0/5 | 20/20 |
| Distant scene 160×120 | Astra 6 | 1/15 | 0/5 | 0/5 | 18/20 |
| Distant scene 160×120 | Gemini 3.6 Flash (Innate proxy) | 5/15 | 0/5 | 4/5 | 8/20 |
| Distant scene 160×120 | PR #807 isolated face path | 0/15 | 0/5 | 0/5 | 20/20 |

## PR #807 with person detection included

The main PR rows above bypass the person detector and run YuNet over the supplied query, then SFace. The following separate diagnostic uses the PR’s HOG person detector and searches the top 40% of its boxes. It includes no outfit fallback, temporal enrollment, roster updates or robot integration.

| Condition | Method | Known correct | Unknown rejected | Unknown falsely named | Abstained |
|---|---|---:|---:|---:|---:|
| Near scene 320×240 | PR HOG → YuNet → SFace | 0/15 | 0/5 | 0/5 | 19/20 |
| Near scene 160×120 | PR HOG → YuNet → SFace | 0/15 | 0/5 | 0/5 | 20/20 |
| Distant scene 320×240 | PR HOG → YuNet → SFace | 0/15 | 0/5 | 0/5 | 20/20 |
| Distant scene 160×120 | PR HOG → YuNet → SFace | 0/15 | 0/5 | 0/5 | 20/20 |

HOG-plus-face outcomes: {'no_person': 47, 'below_24px': 28, 'no_usable_face': 4, 'score_or_margin_reject': 1}. No enrolled character was correctly recognized in this scene diagnostic.

## Timing and validity

| Method | Completed cases | Errors | Median latency | p95 latency |
|---|---:|---:|---:|---:|
| Astra 6 | 140 | 0 | 3684.1 ms | 5925.3 ms |
| Gemini 3.6 Flash (Innate proxy) | 140 | 0 | 3245.4 ms | 5097.5 ms |
| PR #807 isolated face path | 140 | 0 | 2.0 ms | 24.8 ms |
| PR with HOG (scene cases only) | 80 | 0 | 8.6 ms | 18.2 ms |

LLM timings measure API round trips, including network and provider processing. Local timings measure image decode, detection, alignment, embedding and matching as applicable, with models and gallery already loaded, on this Mac CPU. The runner requested one OpenCV thread, but the saved runtime configuration reported 10 threads; single-thread timing is not established. These are different execution environments. The PR’s fast aggregate median includes many early rejections before SFace executes.

For the 35 PR queries that actually produced a face embedding, median local latency was 21.0 ms.
- Astra 6: returned model identifiers `['gpt-6-astra']`. Raw provider token usage is saved per request; proxy dollar pricing is not inferred.
- Gemini 3.6 Flash (Innate proxy): returned model identifiers `['gemini-3.6-flash']`. Raw provider token usage is saved per request; proxy dollar pricing is not inferred.

## Interpretation and limitations

- The face-only tests directly exercise synthetic facial appearance matching across new views. The scene tests also measure whether the model can find and use a tiny face. They are not body-only or clothing-only recognition tests.
- Four of the five held-out designs deliberately resemble gallery characters in broad appearance. Inspection also reveals possible generator convergence between some pairs. Their intended labels come from the generation specification, not verified distinct real identities. False matches therefore combine system confusion with synthetic identity ambiguity. Do not use these counts as real-world false-acceptance estimates.
- Only 20 unique fictional characters and five unique unknowns are present; resolution variants and views are correlated. The condition weights in the overall JSON summary are arbitrary. No significance claim or rare-error guarantee is warranted.
- Full-scene views and enrollment portraits were created within the same source-sheet generation. The separate face-only query sheet was generated using those sheets as references. This can exaggerate visual consistency. Conversely, generator drift can create label noise.
- PR thresholds were unchanged: face cosine ≥0.42, margin ≥0.05, YuNet score ≥0.6 and detected face height ≥24 px. No threshold was tuned on test cases. Missing faces are benchmark abstentions; a valid face embedding that fails matching is UNKNOWN. This distinction prevents detection failure from counting as successful unknown rejection.
- Primary PR results use OpenCV 4.12.0.88 / NumPy 2.2.6. An exploratory OpenCV 5 run is retained but excluded: HOG was unavailable in that build, and small-face localization differed. This runtime sensitivity matters when comparing these local numbers with a Jetson installation.
- No real-person facial identification, automatic enrollment, temporal tracking, crowded scenes, severe occlusion, calibrated motion blur or physical robot execution was tested. A potential real-world system remains unvalidated by this experiment.

## Charts

![Correct recognition by image detail](figures/recognition-by-detail.png)

![Unknown-character outcomes](figures/unknown-outcomes.png)

[Diagram and vector exports](figures/README.md).

## Artifacts

- [Inspect all images and predictions](review.html) — self-contained visual report.
- [Reference/query face QA sheet](assets/qa_faces.jpg).
- [Protocol and reproduction](README.md), [manifest](manifest.json), [crop annotations](annotations.json), [generation provenance](generation_provenance.json).
- [Astra predictions](results/astra-low/predictions.csv), [Gemini predictions](results/gemini-low/predictions.csv), [PR face predictions](results/pr807-face/predictions.csv), [PR with HOG](results/pr807-hog-face/predictions.csv).
- [PR #807 source](https://github.com/innate-inc/innate-os/pull/807), pinned to `487978f9fc69bda93b349423076c3455248ae1c2`; source/weight hashes in `pr807/`.
