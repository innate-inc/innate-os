#!/usr/bin/env python3
"""A Claude agent as the robot's brain, over a file bridge.

WHY THIS EXISTS. The scripted oracle proves each challenge's END STATE is
reachable, but it is deaf, its grab is abstract, and it reads waypoints
nobody spoke. A strong general agent attempting the suite honestly -- seeing
only the camera, the brief, the narrator lines and its own skill results --
is a different validity probe: where IT fails, the failure is evidence about
the task or the world, not just about one production brain. This is the
false-negative sweep: challenges that a competent agent cannot pass need
their worlds debugged, and challenges it passes are certified solvable by
something that plays by the robot's rules.

MECHANISM. The standard in-process pipeline is reused wholesale --
BrainAgent, its observation format, its action menu, its judging engine, the
narrator, the think-time hold. The only new part is the backend: each
decision is written to `bridge/req_NNNNN.json` (observation text, menu, and
the path of this turn's camera frame) and the process blocks until a
`bridge/resp_NNNNN.json` appears. Who writes the response is outside this
file's knowledge: in practice a Claude subagent polls the directory, looks
at the frame, and answers. Every exchange is appended to `log.jsonl`, so the
whole run is auditable turn by turn -- what it saw, what it said, what
happened.

SEPARATION. This runs the sim IN PROCESS: no Docker, no rosbridge, no world
server ports, no Gemini key, no Innate proxy. It can run beside the live
evaluation without sharing anything but CPU cores.

FIDELITY RULES, so the numbers mean something:
  * The agent gets the standard menu, the standard 40-turn budget and the
    challenge's own time limit -- not the live run's tighter wall caps,
    because a probe for false negatives must not add ways to fail.
  * Camera at the real 640x480.
  * Nothing is added to the observation that the production brain would not
    have. The responder is instructed to read ONLY req/resp/frame files.

Usage:
    claude_bridge.py --chunk chunk.txt [--bridge DIR] [--out DIR]
        chunk.txt: lines of "<map> <challenge_id>"; runs them in order,
        appends rows to <out>/episodes.jsonl, then writes <out>/done_<chunk>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))

RESP_TIMEOUT_S = 1500.0  # a subagent turn can be slow; a wedged one should not hang the sweep forever
POLL_S = 0.5


class ClaudeBridgeBackend:
    """decide() = write a request file, block until the response file appears."""

    wants_image = True
    # Sim time charged per model call: a realistic strong-model latency,
    # instead of the bridge's real wall time (file polling + a subagent's
    # deliberation), which billed one turn at 295 s and turned solved
    # episodes into timeouts. See runner.py's think-charge note.
    think_charge_s = 8.0
    # And the stall-breaker must exceed the bridge's own response timeout,
    # or a legitimately slow turn is reported as a hung backend.
    think_wall_cap_s = RESP_TIMEOUT_S + 120.0

    def __init__(self, bridge: Path, log: Path, map_name: str, challenge_id: str) -> None:
        self.bridge, self.log = bridge, log
        self.map_name, self.challenge_id = map_name, challenge_id
        self.turn_in_episode = 0

    def _next_n(self) -> int:
        ns = [int(p.stem.split("_")[1]) for p in self.bridge.glob("req_*.json")]
        return (max(ns) + 1) if ns else 1

    def _log(self, kind: str, payload: dict) -> None:
        row = {
            "t_wall": round(time.time(), 1),
            "kind": kind,
            "map": self.map_name,
            "challenge": self.challenge_id,
            **payload,
        }
        with self.log.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def decide(self, obs, menu) -> dict:
        n = self._next_n()
        self.turn_in_episode += 1
        req = {
            "n": n,
            "map": self.map_name,
            "challenge": self.challenge_id,
            "turn_in_episode": self.turn_in_episode,
            "observation": obs.as_text(),
            "image": obs.image_path,
            "menu": menu,
        }
        tmp = self.bridge / f".req_{n:05d}.tmp"
        tmp.write_text(json.dumps(req, indent=1))
        tmp.rename(self.bridge / f"req_{n:05d}.json")  # atomic: no half-read requests
        (self.bridge / "current.json").write_text(json.dumps(req, indent=1))
        self._log("req", {"n": n, "turn": self.turn_in_episode, "observation": obs.as_text(), "image": obs.image_path})

        resp_path = self.bridge / f"resp_{n:05d}.json"
        deadline = time.time() + RESP_TIMEOUT_S
        while not resp_path.exists():
            if time.time() > deadline:
                self._log("timeout", {"n": n})
                raise RuntimeError(f"no response to req {n} within {RESP_TIMEOUT_S:.0f}s")
            time.sleep(POLL_S)
        # The writer may not be atomic; tolerate a half-written file once.
        for attempt in (1, 2):
            try:
                data = json.loads(resp_path.read_text())
                break
            except json.JSONDecodeError:
                if attempt == 2:
                    raise
                time.sleep(1.0)
        action = str(data.get("action", "")).lower()
        args = data.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        self._log("resp", {"n": n, "action": action, "args": args})
        return {"action": action, "args": args}


def _run_one(map_name: str, cid: str, bridge: Path, out: Path) -> int:
    """One episode, in THIS process. Called only in a fresh child: the room
    and prop registries load once per process, so a driver that runs two maps
    in one process serves every later map the FIRST map's world -- observed
    as a 'pantry' that rendered the counter cafe, and camera static once the
    engine dropped props the loaded world had never heard of. main.py always
    forks a worker per episode; this file learned the same rule the hard way."""
    log = out / "log.jsonl"

    from brain_agent import BrainAgent
    from runner import run_episode

    def make(ch):
        agent = BrainAgent(ClaudeBridgeBackend(bridge, log, map_name, cid))
        agent.name = "brain:claude"
        # The turn cap exists to stop loops, not to bind before the sim clock:
        # a probe aborted a completable evacuation leg with sim time to spare
        # because 40 turns would not fit it. Scale with the challenge's own
        # budget at ~9 s of charged time per turn; 40 stays the floor.
        agent.max_turns = max(40, int((ch.time_limit_s or 400) / 9))
        return agent

    ep = run_episode(map_name, cid, make, max_sim_s=None, render_wh=(640, 480))
    with (out / "episodes.jsonl").open("a") as fh:
        fh.write(json.dumps(asdict(ep)) + "\n")
    with log.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "t_wall": round(time.time(), 1),
                    "kind": "episode_end",
                    "map": map_name,
                    "challenge": cid,
                    "passed": ep.passed,
                    "goals": f"{ep.goals_done}/{ep.goals_total}",
                    "reason": ep.error or ep.reason,
                }
            )
            + "\n"
        )
    print(
        f"        -> {'PASS' if ep.passed else 'fail'} {ep.goals_done}/{ep.goals_total} "
        f"sim {ep.elapsed_s:.0f}s  {ep.error or ep.reason}",
        flush=True,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=Path)
    ap.add_argument("--one", nargs=2, metavar=("MAP", "CHALLENGE"))
    ap.add_argument("--bridge", type=Path, default=BENCH / "results" / "claude_probe" / "bridge")
    ap.add_argument("--out", type=Path, default=BENCH / "results" / "claude_probe")
    args = ap.parse_args()

    args.bridge.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)

    if args.one:
        return _run_one(args.one[0], args.one[1], args.bridge, args.out)
    if not args.chunk:
        print("need --chunk or --one")
        return 1

    jobs = []
    for line in args.chunk.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            m, c = line.split()
            jobs.append((m, c))

    # One CHILD PROCESS per episode -- see _run_one for why this is load-bearing.
    import subprocess

    results = []
    for i, (map_name, cid) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {map_name} {cid} ...", flush=True)
        r = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--one",
                map_name,
                cid,
                "--bridge",
                str(args.bridge),
                "--out",
                str(args.out),
            ],
            env=os.environ.copy(),
        )
        results.append((map_name, cid, r.returncode))
        if r.returncode != 0:
            print(f"        episode child exited {r.returncode}", flush=True)

    (args.out / f"done_{args.chunk.stem}.json").write_text(
        json.dumps(
            {
                "chunk": args.chunk.stem,
                "episodes": [{"map": m, "challenge": c, "child_rc": rc} for m, c, rc in results],
            },
            indent=1,
        )
    )
    print(f"chunk {args.chunk.stem} complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
