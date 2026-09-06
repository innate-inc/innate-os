# Pickup comparison

These scripts run the ordinary ROS `innate-os/pick_any_object` action in a local
simulator. They make billed Gemini/Astra requests. Keep the autonomous brain
inactive, use an isolated port range, and coordinate other simulator CPU load.
They do not support physical robots.

Run from a clean candidate checkout using the simulator Python environment:

```sh
sim/.venv/bin/python scripts/experiments/pickup/run_trial.py matched-lego-r1-classic \
  --live --container innate-dev-EXACT-ID --port-base 30600 \
  --output-root /absolute/evidence/directory --max-provider-calls 290 \
  --campaign matched-v1 --scenario-id onboarding-lego --repeat 1 --controller classic
```

Use `scenarios.json` unchanged for the three scenarios. Run three repeats each,
alternating classic/Astra order per pair. Both controllers use the same checkout,
fixture, rates, limits, reset state, 180-second deadline and judge. Record
`--controller astra` for the candidate. The default ordinary skill caller uses
Astra; the explicit classic option retains the working comparison implementation.

The runner temporarily appends a data-only phase/proxy wrapper and copies
`probe.py` beside the skill. It records exact source snapshots and hashes, then
restores the original source. If the runner is forcibly killed, inspect the
recorded `original_pick_any_object.py` and the worktree before restoring it;
never overwrite an intervening edit. The benchmark helper must not be shipped in
`workspace/innate_skills/`. No provider budget applies to the production skill.

The cumulative benchmark ledger is in `workspace/skill_storage/pickup_probe`.
Its configured call limit and $5 estimated-cost review threshold stop subsequent
requests. Raw usage includes thinking and cache-write tokens. Missing usage or
provider failures require inspection before another billed attempt. These are
list-price estimates, not invoices or a provider-enforced billing limit.

After each run:

```sh
sim/.venv/bin/python scripts/experiments/pickup/judge.py /absolute/evidence/directory/TRIAL
```

After all 18 matched trials:

```sh
sim/.venv/bin/python scripts/experiments/pickup/report.py /absolute/evidence/directory --campaign matched-v1
```

Every failed attempt contributes the fixed 180-second penalty. Both successful
and all-attempt medians must improve by at least 50%; baseline must succeed in
at least two of three repeats of each scenario, with no observed per-scenario
reliability regression. Report all attempts and the limits of a small simulator
sample. Development pilots and negative tests stay outside that comparison.
The runner records22seconds after action completion. A20second stable-retention
gate additionally rejects delayed drops in both controllers; this durability
check is reported separately from the fixed action-plus-two-second latency.
Use `--cancel-during-astra` for the real inference cancellation check, recording
its acknowledgement, action status, late provider completion and joint history.

Use `--cancel-during-wrist` to request Stop half a second after wrist localization,
then verify the actual joint trace and that no grasp starts after cancellation.
