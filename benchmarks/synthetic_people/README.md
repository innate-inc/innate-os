# Synthetic fictional-character recognition

This benchmark uses only newly generated fictional adults and invented names. There are no real-person reference photographs or robot recordings in the scored data. The user's robot video informed the visual style only.

The intended task is choosing one of 15 reference characters or rejecting an unknown character. Five additional fictional characters are held out. Clothing is shared across characters and changes between reference and query. Face-only crops remove clothing entirely. Both known and unknown characters occur across generator batches.

Two kinds of test are kept distinct:

- Controlled face-only resolution: independently rendered low-angle faces at 64, 32 and 16 pixels square. These sizes are crop dimensions, not detected face heights and not calibrated physical distances.
- Robot-style scenes: near and distant low-angle full-body views, at 320×240 and 160×120. Source frames are generated; distance, optics, camera height and lighting are illustrative rather than calibrated.

Astra (`gpt-6-astra`) and Gemini (`gemini-3.6-flash` through the Innate proxy) receive the same 15 named face references and one query per request. Gallery order is shuffled for each case, identically across systems. Query filenames, truth labels, generator sheets, descriptions and unknown enrollment portraits are not included in requests. Low reasoning/thinking settings match the preceding outfit experiment. Model errors and abstentions are reported separately and count as incorrect overall.

PR #807 is pinned to `487978f9fc69bda93b349423076c3455248ae1c2`. Its YuNet face localization, SFace alignment/embedding and cosine matching definitions are extracted with verified identical ASTs. Its face score threshold 0.42, margin 0.05, minimum face height 24px and YuNet score threshold 0.6 remain unchanged. The 15 reference vectors are bootstrapped directly from the synthetic gallery. No automatic enrollment, roster adaptation or outfit fallback is included. A scene-only diagnostic includes the PR's HOG detector and upper-40%-of-person-box face search. This is a static face-path comparison, not validation of the complete temporal robot system.

All source sheets, generation prompts, crop coordinates, source hashes, processed image hashes, predictions and run settings are retained. Pixel processing only crops, resizes and JPEG-encodes generated assets; it does not enhance or hallucinate detail.

Limitations: only 20 fictional characters, repeated in seven conditions. Generator identity drift can create label noise, while shared generation can make identity consistency easier than real footage. Hair and coarse appearance remain visible in face crops. This is not a balanced demographic study; no subgroup claims are made. Five unique unknowns cannot establish a low false-acceptance rate. The results cannot establish real-person recognition accuracy, robot deployment readiness or performance with crowds, natural motion, severe occlusion, temporal enrollment or an evolving roster.

## Reproduction

Use Python 3.11 with `requirements.txt`. Model weights are pinned and SHA-verified in `pr807/model_provenance.json`. Credentials are read from the repository `.env`; they are not copied into artifacts or logs.

```sh
python benchmarks/synthetic_people/benchmark.py --provider astra --model gpt-6-astra --effort low --run-name astra-new
python benchmarks/synthetic_people/benchmark.py --provider gemini --model gemini-3.6-flash --effort low --run-name gemini-new
python benchmarks/synthetic_people/run_pr807.py --mode face
python benchmarks/synthetic_people/run_pr807.py --mode hog-face
```

API runs can resume with the same run name when hashes and settings agree. Local PR runs refuse to overwrite their completed result folders; move them to a new retained folder before rerunning. `prepare.py` refuses to overwrite a frozen manifest. Tests are in `test_protocol.py` (pytest).

The primary local results use OpenCV 4.12.0.88 and NumPy 2.2.6, on this Mac. The runner requested one OpenCV thread, but the saved runtime configuration reported 10 threads; these are not verified single-thread timings. An initial OpenCV 5.0 run is retained as `pr807-face-opencv5-exploratory`; that build lacked the PR's HOG API and produced different small-face detection behavior. It is excluded from primary comparisons. Dataset face-box annotations were prepared with OpenCV 5 before scoring and then frozen for every evaluated system.

PR missing-face outcomes map to benchmark `UNCERTAIN`. A computed face vector rejected by the cosine/margin rule maps to `UNKNOWN`. Accepted vectors map to the invented reference name. The PR itself represents unrecognized sightings as `unknown`; it does not distinguish these two cases, and HOG misses can produce no sighting. The benchmark preserves diagnostic reasons separately.
