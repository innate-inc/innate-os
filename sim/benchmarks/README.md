# Navigation benchmark (sim)

40 scenarios in the sim apartment. A scenario is a spawn pose, an instruction,
and a goal; the runner places the robot, sends the instruction to the policy,
and scores where it stopped.

Scenarios come in sets that share a spawn and differ **only in the
instruction**. Overall success says how good a policy is; comparing siblings
says whether the words mattered at all — a policy that ignores instructions
cannot finish nearer the goal it was sent to than the goal the other
instruction from that same pose named, except by chance.

The set is generated from the map (`build_scenarios.py`), so every spawn and
goal has clearance and every pair is connected. It lives at
`webapp/public/nav_benchmark.json`, which is also what the policy page reads.

```bash
# serve a checkpoint, then:
python3 sim/benchmarks/runner.py \
    --scenarios webapp/public/nav_benchmark.json \
    --out sim/benchmarks/results/<label>.json --label <label>
python3 sim/benchmarks/analyze.py "sim/benchmarks/results/*.json"
```

`--server host:port` points one run at a specific policy server, so several
checkpoints can stay loaded and be compared without reloading between runs.

Scoring mirrors the closed-loop eval harness: success is stopping inside the
goal radius, oracle is ever being inside it. The radius here is 0.75 m, not the
harness's 3 m — the whole navigable area of this flat is about 14 m², so 3 m
would count almost any position as an arrival.
