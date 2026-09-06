# Household Orders: preserve progress on a mission retry

The previous `mission_run({})` always created a new mission. Repeating it during recovery made identity and search initialize again and made a confirmed order disappear. The candidate resumes the active Household Orders mission by default. A new task uses `restart=true`; recovery also repeats the already-idempotent identity/search initializers, covering interrupted startup.

This is a measured repair of that failure mechanism. Full challenge completion rate and latency on the new stack remain unmeasured.

## Stack and scope

This draft is above [#772](https://github.com/innate-inc/innate-os/pull/772), foundation `cc039cc4687ba365625135979299253caee275aa`. The first commit ports the existing Household Orders agent/skills from [#680](https://github.com/innate-inc/innate-os/pull/680), fast observation/interaction guards from [#694](https://github.com/innate-inc/innate-os/pull/694), and coverage scoring from [#698](https://github.com/innate-inc/innate-os/pull/698). Those features were absent from the foundation's current-main base. It retains current-main Nav2 keepout diagnostics and safe-approach hints, and keeps low-rate map/mask/AMCL feeds available while idle. Six navigation seam tests cover that integration. The combined face-identity experiment from #724 is not imported.

The second commit is the new mission-continuity fix and its focused tests. Tested runtime revision: `64d90a5be2fc5e8803f41489ae7c7377ee4a700e`. Rebase onto the final foundation added documentation only; runtime files are unchanged from the tested pre-rebase tree. Everything here is local/draft; nothing was merged or deployed.

## Current evidence

| Check | Baseline | Candidate |
|---|---|---|
| Actual running ROS skill-server replay, three repetitions | 3/3 retries changed run ID and returned `NOTE_MISSING` | 3/3 kept the run ID and exact `NOTE_FOUND` value |
| Identity and search after the same retry | Reinitialized | Returned `ROSTER_ALREADY_INITIALIZED` and `SEARCH_ALREADY_INITIALIZED` |
| Explicit new task | Fresh run | Three distinct initial run IDs; no prior notes inherited |
| Fixed-frame Astra startup | Correct three-step initialization | Correct three-step initialization, including `restart=true` |
| Fixed-frame Astra lost-context recovery | Listed saved notes, then selected search | Resumed the mission, listed saved notes, then selected search |

The real ROS replay used `execute_skill` with the autonomous brain disabled. It initialized a mission, identity and search, stored the exact synthetic order `no onions; sauce on the side`, repeated `mission_run({})`, repeated identity/search initialization, and read the note. Full result receipts and all IDs are in [baseline-rpc.json](baseline-rpc.json) and [candidate-rpc.json](candidate-rpc.json). It did not move the robot, recognize a real resident, or place an order.

The first eleven paid calls used the baseline and initial candidate prompts. Final review added identity/search initialization to the recovery instructions so a partially initialized run cannot reuse previous-mission state. The twelfth call used this final prompt and correctly selected identity `begin` after a recorded `RUN_RESUMED` receipt. The entire final recovery sequence is covered by the scripted ROS replay and an interrupted-startup regression test; it was not rerun as a full model conversation. Both exact prompt versions, tool metadata and image hashes are retained in [probe-inputs.json](probe-inputs.json).

All 12 evaluated model decisions passed their stated criteria. Both arms used `gpt-6-astra`, low reasoning, the same 640×480 simulator image and the foundation's `OpenAIContext`/managed transport. They received recorded real ROS results; model-selected actions were not dispatched. Each case stopped when it reached an action with no recorded result. No seed was set and there were no automatic retries. These small controlled fixtures demonstrate compatibility, not a success-rate improvement or a latency comparison. Raw usage in [astra-replay.json](astra-replay.json) and [astra-final-prompt.json](astra-final-prompt.json) totals **$0.2898135 estimated**, including cache writes, within the authorized 12-call/$6 envelope. This is token-price estimation, not a billing invoice.

Validation: **265 tests passed** across mission continuity/notes, resident identity, agent loading, brain guards, OpenAI context/transport, pose math, navigation keepout and state QoS. Ruff and diff checks passed. All 20 ROS packages built and the local simulator's actual skill server accepted the changed contract through hot reload. The tests cover exact state/archive preservation, fresh tasks, interrupted initialization, concurrent process initialization, wrong-owner protection, invalid booleans, and cancellation before mutation. Repeated `restart=true` remains intentionally destructive; the prompt restricts it to new tasks.

## Recovered historical context

Eighty original GCS manifests and results were recovered and checked before choosing this repair. [historical-runs.csv](historical-runs.csv) retains each result ID, replicate index, manifest hash and outcome; [historical-summary.json](historical-summary.json) contains the distributions.

| Historical source | Successes | Median successful time | p95 successful time |
|---|---:|---:|---:|
| `95a493880b797e9281afe2af92d7b674c1cc44a0` | 40/40 | 310.3 s | 559.425 s |
| Combined facefix `29f2dedced1c7f1fdc430f973d5a95710a5fd8f9` | 22/40 | 599.1 s | 881.58 s |

All 18 failures reached the 900-second time limit; seven also had two mission runs, invalidating mission evidence. Retained traces for r20/r23/r33 show empty-argument `mission_run` retries at t+481/556/601 followed by fresh initialization. This supports the reset diagnosis but does not attribute every failure to it.

Original evidence bucket: `gs://innate-managed-infra-household-orders-usw1-59581386846`. Manifests are `manifests/route-removed-confirm-20260824-r01.json` through r40 and `manifests/facefix-20260826-r01.json` through r40. Results are `results/<run_id>/result.json`. Manifest hashes use SHA-256 of sorted, compact JSON. The original runner is `inputs/benchmark-challenge-0453dc87.py`, SHA-256 `0453dc87a91265678cd06ed34facf5482ab2f45769adf0d4429c855815151f2a`.

Historical runs used Gemini 3.6 Flash/low, fresh e2-standard-8 VMs and zero retries. Replicate indices are not controlled seeds. Current main also changes Casey's orientation from −90° to +90°. Therefore the historical 40/40 is **not** a reproduced score of this port, and it must not be compared as the control arm for the new Astra stack. No cloud VMs were created for this work.
