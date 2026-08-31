"""Run challenges against a LIVE innate-os stack, judged by the same engine.

This is the half of the harness that makes it agent-agnostic. `runner.py` drives
a Python agent against VirtualMars in-process, which is fast and parallel but is
NOT the shipping system. This one connects to the world server the real stack
already exposes, starts a challenge, and reads the verdict off the state stream:

    ws://127.0.0.1:8800
      -> {"op": "start_challenge", "id": ...}
      <- {... "challenge": {"state": running|passed|failed, "goals": [...], ...}}

The robot is driven by whatever is attached to that stack -- the Gemini brain
today, a different model, a different context policy, or a whole new
architecture tomorrow. None of that is visible here, which is the point:
swapping the brain must not mean forking the harness.

The judge is identical in both paths (mars_sim_driver.challenges), so scores
are comparable. What is NOT comparable is wall time: this runs at real time
against a live stack, one episode at a time, while the in-process path runs
many episodes faster than real time.

The robot is never told it is being tested -- the brain sees the challenge's
`brief` as an ordinary instruction and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import Episode  # noqa: E402

# Steps at or above this are a teleport or a dropped frame, not driving: the
# world reset between challenges puts the robot back at spawn. Clamped rather
# than discarded -- see _LiveProbe.
TELEPORT_STEP_M = 0.5
# Below this, it is physics chatter. At 5 Hz a 480 s episode is 2400 samples,
# so 1 mm each would invent ~2 m of path.
JITTER_M = 0.002
# The same table spend.py uses, imported would be better but spend.py runs as a
# script; keep them equal or the two tools disagree on the same log.
PRICES = {"gemini-3.6-flash": (0.75e-6, 3.75e-6), "gemini-3.5-flash": (1.50e-6, 9.00e-6)}
DEFAULT_PRICE = (0.75e-6, 3.75e-6)

DEFAULT_URL = "ws://127.0.0.1:8800"
DEFAULT_ROSBRIDGE = "ws://127.0.0.1:9090"
CHAT_IN = "/brain/chat_in"
# The helpers live beside this file in the repo. They were read from $HOME,
# which silently used whatever stale copy happened to be there.
_BENCH = Path(__file__).resolve().parent
PRIME_SH = _BENCH / "prime_brain.sh"
SAY_SH = _BENCH / "say_brief.sh"


def prime(reset: bool = False) -> str:
    """Put the brain in a state where it can act: activate, enable skills, and
    optionally clear its conversation first.

    Shelled out to ros2 rather than driven over rosbridge. Service calls through
    rosbridge timed out here (topic publishes were fine), and a flaky setup step
    is worse than a slow one: an un-activated brain silently discards every
    instruction, so the whole sweep scores zero for the wrong reason.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["bash", str(PRIME_SH)] + (["--reset"] if reset else []), capture_output=True, text=True, timeout=240
        )
        return "" if r.returncode == 0 else (r.stderr or r.stdout or "")[-200:]
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def instruct(text: str) -> str:
    """Give the robot the challenge's brief, as a person would.

    Starting a challenge only builds the SCENE. The brief is written for a human
    who then tells the robot what to do, so nothing instructs the agent: the
    first live run sat for 420s and the brain never made a single model call.
    The judge is deliberately blind to this -- goals are checked against ground
    truth and the robot is never told it is being tested -- so the harness has
    to speak, and it says exactly the brief and nothing else.
    """
    # Shelled out to ros2, NOT published over rosbridge. The rosbridge
    # advertise+publish reported success and the brain never logged a single
    # "User message" -- a silent no-op, which is the worst failure available
    # here: the agent sits idle and the whole sweep scores zero with nothing
    # anywhere saying why. chat_in also needs a JSON envelope inside the
    # String; raw text is dropped as "invalid JSON payload".
    import subprocess

    try:
        r = subprocess.run(["bash", str(SAY_SH)], input=text, capture_output=True, text=True, timeout=120)
        return "" if r.returncode == 0 else (r.stderr or r.stdout or "")[-200:]
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


class _LiveProbe(threading.Thread):
    """Measure what the robot DID during one episode, not just whether it won.

    The live path recorded pass/fail and goals and left every measured field at
    zero -- turns, path_len_m, utterances, heard are filled only by the
    in-process runner. So in the results file a robot that never moved and one
    that drove four metres into a wall are the same row: `0/2, timeout`. Every
    diagnosis then needs the ROS logs, which are per-map and interleave all the
    episodes on that map.

    Reads the same rosbridge the challenge engine uses. Entirely best-effort:
    an episode must never fail because its instrumentation did.
    """

    def __init__(self, rosbridge: str) -> None:
        # daemon via the constructor, not a class attribute: `daemon = True` on
        # the class shadows Thread's property, so _daemonic stays False and a
        # later `probe.daemon = False` is silently accepted as a plain
        # attribute that nothing reads.
        super().__init__(daemon=True)
        self._url = rosbridge
        # NOT self._stop. threading.Thread already defines a private _stop()
        # method that join() calls internally, so shadowing it with an Event
        # makes every join() raise "'Event' object is not callable" -- which
        # killed live_runner on the first episode of every category, silently,
        # while the brain kept billing.
        self._finished = threading.Event()
        self.path_m = 0.0
        self.utterances = 0
        self.first_utterance_s: float | None = None
        self.error: str | None = None
        self.dropped_frames = 0
        # Stamped when the thread actually starts, not at construction: the
        # episode still has to connect to the world server, start the
        # challenge and run a blocking `instruct` subprocess, and counting
        # that as part of time-to-first-word inflates it by several seconds.
        self._t0 = time.time()

    def start(self) -> None:
        self._t0 = time.time()
        super().start()

    def run(self) -> None:
        try:
            from websockets.sync.client import connect  # noqa: PLC0415

            with connect(self._url, open_timeout=10, max_size=None) as ws:
                ws.send(
                    json.dumps({"op": "subscribe", "topic": "/odom", "type": "nav_msgs/Odometry", "throttle_rate": 200})
                )
                ws.send(json.dumps({"op": "subscribe", "topic": "/brain/chat_out", "type": "std_msgs/String"}))
                last: tuple[float, float] | None = None
                while not self._finished.is_set():
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    # ONE BAD FRAME COSTS THAT FRAME, NOT THE CONNECTION. This
                    # parse used to sit bare in the loop, so a single malformed
                    # message ended the thread for the rest of the episode --
                    # and a dead probe records path 0.0 with 0 utterances,
                    # which in the results file is indistinguishable from a
                    # robot that never moved. That is the exact confusion this
                    # class exists to end. The challenge engine's own
                    # subscriber guards per message for the same reason.
                    try:
                        frame = json.loads(raw)
                        topic, msg = frame.get("topic"), frame.get("msg")
                        if topic == "/odom" and msg:
                            p = msg["pose"]["pose"]["position"]
                            here = (p["x"], p["y"])
                            if last is not None:
                                step = math.hypot(here[0] - last[0], here[1] - last[1])
                                # CLAMPED, not discarded. A big step is either
                                # the between-challenge teleport back to spawn
                                # or a dropped frame; throwing the whole gap
                                # away recorded a real 3 m drive as 0 m when
                                # samples landed 0.6 m apart. Clamping keeps a
                                # dropped frame roughly honest while still
                                # refusing to bill a teleport as driving.
                                # Steps below the dead-band are physics chatter:
                                # at 5 Hz a 480 s episode is 2400 samples, so
                                # 1 mm each is ~2 m of fictional path.
                                if step >= JITTER_M:
                                    self.path_m += min(step, TELEPORT_STEP_M)
                            last = here
                        elif topic == "/brain/chat_out" and msg:
                            said = json.loads(msg["data"])
                            # ONLY the robot speaking. `sender` is one of
                            # robot | robot_thoughts | system | skill_output,
                            # and skill_output fires on every skill result --
                            # so "not system and not user" counted tool output
                            # and inner monologue as utterances.
                            if said.get("sender") == "robot" and str(said.get("text", "")).strip():
                                self.utterances += 1
                                if self.first_utterance_s is None:
                                    self.first_utterance_s = round(time.time() - self._t0, 1)
                    except Exception as exc:  # noqa: BLE001
                        self.dropped_frames += 1
                        if self.error is None:
                            self.error = f"{type(exc).__name__}: {exc}"[:80]
        except Exception as exc:  # noqa: BLE001 -- never fail an episode over telemetry
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self._finished.set()


def _usage_between(t0: float, t1: float) -> tuple[int, int, int, float]:
    """(calls, input tokens, output tokens, cost) billed inside a window.

    Attributes spend to the episode that caused it. The log is append-only with
    a wall-clock stamp per call, so a time window is the whole mechanism -- no
    extra plumbing through the brain, and it picks up BOTH seams (turn stream
    and skill vision) because both write to the same file.
    """
    log = Path(__file__).resolve().parents[1].parent / "workspace/gemini_usage.jsonl"
    calls = tin = tout = 0
    cost = 0.0
    try:
        # errors="replace" and a per-row guard: two root-owned processes append
        # to this file concurrently, so a torn line or a stray byte is a normal
        # event. Every failure mode here used to raise OUTSIDE the episode's
        # try/except and take down the whole category -- a bad decode, a
        # non-numeric `t`, or a null token count.
        with log.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    stamp = float(row.get("t", 0.0))
                    if not (t0 <= stamp <= t1):
                        continue
                    prompt = int(row.get("prompt") or 0)
                    output = int(row.get("output") or 0) + int(row.get("thoughts") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                calls += 1
                tin += prompt
                tout += output
                # Priced PER MODEL, from the same table spend.py uses. A flat
                # flash-3.6 rate under-charged every skill-vision row, which
                # bills at twice that, and made the two tools disagree on the
                # same log by construction.
                rate_in, rate_out = PRICES.get(row.get("model", ""), DEFAULT_PRICE)
                cost += prompt * rate_in + output * rate_out
    except OSError:
        pass
    return calls, tin, tout, round(cost, 4)


async def _episode(
    url: str, challenge_id: str, timeout_s: float, poll_s: float = 0.5, rosbridge: str | None = None, brief: str = ""
) -> Episode:
    import websockets

    wall0 = time.time()
    probe = _LiveProbe(rosbridge) if rosbridge else None
    if probe:
        probe.start()
    ep = Episode("live", challenge_id, "system", False, 0, 0, 0.0, "", 0.0, 0)
    try:
        async with websockets.connect(url, max_size=None, ping_interval=None, open_timeout=15) as ws:
            # First frame is the roster (props + challenges), sent once per
            # observer connection.
            roster = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            ids = {c["id"] for c in roster.get("challenges", [])}
            if challenge_id not in ids:
                ep.error = f"{challenge_id!r} not on the live server's roster"
                ep.wall_s = round(time.time() - wall0, 1)
                return ep

            await ws.send(json.dumps({"op": "start_challenge", "id": challenge_id}))
            deadline = time.time() + timeout_s
            last = None
            # The stream is latest-wins and the server keeps publishing the
            # PREVIOUS run's terminal block until the new one is live. Reading
            # the first block that arrives therefore reports the last episode's
            # verdict as this one's -- observed as a "result" in 0.0s wall with
            # a 300s elapsed time that belonged to an earlier attempt. So: wait
            # for a running block for THIS id before believing anything.
            started = False
            grace = time.time() + 20.0
            cues_sent = 0
            while time.time() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=poll_s + 5))
                except asyncio.TimeoutError:
                    ep.error = "state stream went quiet"
                    break
                block = (msg or {}).get("challenge") or {}
                active = block.get("active") or block
                if not active or "state" not in active:
                    continue
                if not started:
                    same = active.get("id") in (None, challenge_id)
                    if active.get("state") == "running" and same:
                        started = True
                        # Only once the scene is actually up: instructing before
                        # the drops land tells the robot to fetch something that
                        # is still parked off-map.
                        if rosbridge and brief:
                            err = await instruct(brief)
                            print(f"      instructed via {CHAT_IN}" + (f" (FAILED: {err})" if err else ""), flush=True)
                            if err:
                                # The robot was never told the task. Whatever it
                                # does now is not an answer to this challenge, so
                                # the episode is harness-blocked, not a robot
                                # zero -- `scored` below drops blocked episodes.
                                ep.blocked = f"harness: brief not delivered ({err[:80]})"
                    elif time.time() > grace:
                        ep.error = "challenge never entered 'running' (start refused?)"
                        break
                    else:
                        continue
                last = active
                if active.get("state") != "running":
                    break
                # Narrator lines ride the state stream, but a live robot only
                # hears what is put on its wire. runner.py wires the engine's
                # cue sink straight into the in-process agent (runner.py:191);
                # this path never did, so every scripted challenge ran live
                # against a robot that was deaf to its own script -- the
                # correction, the second order, the clarification answer and
                # the blaze urgency lines all fired into the transcript and
                # went nowhere. Forward each new line down the same channel as
                # the brief. "ambient" lines go verbatim too: addressed speech
                # and overheard speech arriving indistinguishably is the whole
                # design of counter_not_for_you.
                for line in (active.get("transcript") or [])[cues_sent:]:
                    cues_sent += 1
                    text = str(line.get("text", ""))
                    if rosbridge and text:
                        err = await instruct(text)
                        print(
                            f"      narrator +{line.get('t')}s ({line.get('kind')}): {text!r}"
                            + (f" (DELIVERY FAILED: {err})" if err else ""),
                            flush=True,
                        )
                        if err and not ep.blocked:
                            # The challenge promised this line -- a fire
                            # warning, a correction, a second order -- and the
                            # robot never heard it. Scoring the episode now
                            # measures the transport, not the robot. Same rule
                            # the brief follows above.
                            ep.blocked = f"harness: narrator line not delivered ({err[:60]})"

            if last:
                goals = last.get("goals", [])
                ep.goals_total = len(goals)
                ep.goals_done = sum(1 for g in goals if g.get("done"))
                ep.elapsed_s = float(last.get("elapsed_s") or 0.0)
                ep.passed = last.get("state") == "passed"
                ep.reason = last.get("reason") or ""
                if last.get("state") == "running":
                    ep.reason = "harness timeout (challenge still running)"
            else:
                ep.error = ep.error or "no challenge block on the stream"

            # Leave the stack clean for the next episode.
            with_suppress = json.dumps({"op": "abort_challenge"})
            try:
                await ws.send(with_suppress)
            except Exception:  # noqa: BLE001 -- best effort; the run is already recorded
                pass
    except Exception as exc:  # noqa: BLE001 -- one bad episode must not sink the sweep
        ep.error = f"{type(exc).__name__}: {exc}"

    if probe:
        probe.stop()
        probe.join(timeout=3.0)
        ep.path_len_m = round(probe.path_m, 2)
        ep.utterances = probe.utterances
        ep.first_utterance_s = probe.first_utterance_s
        if probe.error and not ep.error:
            print(f"      (probe: {probe.error})", flush=True)
    ep.model_calls, ep.tokens_in, ep.tokens_out, ep.cost_usd = _usage_between(wall0, time.time())
    ep.wall_s = round(time.time() - wall0, 1)
    return ep


async def _sweep(
    url: str,
    ids: list[str],
    timeout_s: float,
    out: Path,
    rosbridge: str | None = None,
    briefs: dict[str, str] | None = None,
    blocked: dict[str, str] | None = None,
) -> list[Episode]:
    results: list[Episode] = []
    for n, cid in enumerate(ids, 1):
        why = (blocked or {}).get(cid)
        if why:
            # Not attempted, and deliberately not scored. Running it would burn
            # a full timeout and hand back a zero that reads as an agent
            # failure, which is the opposite of what happened.
            ep = Episode("live", cid, "system", False, 0, 0, 0.0, "", 0.0, 0, blocked=why)
            results.append(ep)
            print(f"[{n:>3}/{len(ids)}] {ep.as_row()}", flush=True)
            out.write_text(json.dumps([asdict(r) for r in results], indent=1))
            continue
        if rosbridge:
            # Fresh agent per challenge: the world resets on start, the BRAIN
            # does not, so without this it carries the previous task's history
            # forward and the scores absorb the contamination silently.
            prime_err = prime(reset=True)
            if prime_err:
                print(f"      prime failed: {prime_err}", flush=True)
            await asyncio.sleep(1.0)
        else:
            prime_err = ""
        ep = await _episode(url, cid, timeout_s, rosbridge=rosbridge, brief=(briefs or {}).get(cid, ""))
        if prime_err and not ep.blocked:
            # An unprimed brain is carrying the last challenge's history, so
            # this score would be measuring contamination, not the robot.
            ep.blocked = f"harness: brain not primed ({prime_err[:80]})"
        results.append(ep)
        print(f"[{n:>3}/{len(ids)}] {ep.as_row()}", flush=True)
        out.write_text(json.dumps([asdict(r) for r in results], indent=1))
    return results


def _blocked_here(ids: list[str]) -> dict[str, str]:
    """{challenge id: why it cannot be attempted} for this deployment.

    Read from the challenge definitions on disk rather than the world server's
    roster, because the reason lives in the goals (does an object have to end
    up somewhere?) and the roster only carries briefs. A challenge whose
    definition cannot be found is not blocked -- silence here must never
    invent a block that stops a runnable challenge.

    BOTH roots are loaded. sim/bundles holds this benchmark's 45; sim/challenges
    holds the stock apartment suite, which ChallengeEngine serves whenever the
    loaded assets bundle has no rooms/ dir. Their ids do not overlap, so
    scanning only the bundles made the gate a silent no-op on the stock world --
    every stock challenge that needs a pick then runs to the default 900s
    timeout and produces a zero, which is the exact outcome this exists to
    prevent.
    """
    try:
        # Inside the try with everything else: an ImportError here would kill
        # the sweep, and this whole check is meant to fail open.
        from capabilities import blocked_reason  # noqa: PLC0415

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))
        from mars_sim_driver.challenges import load_challenges  # noqa: PLC0415

        sim = Path(__file__).resolve().parents[1]
        roots = [b / "challenges" for b in sorted((sim / "bundles").iterdir()) if b.is_dir()]
        roots.append(sim / "challenges")
        found: dict = {}
        for root in roots:
            if root.is_dir():
                found.update(load_challenges([root]))
    except Exception as exc:  # noqa: BLE001 -- reported, not silently ignored
        # Failing open here schedules challenges this deployment cannot
        # attempt and counts their zeros, which is the mis-scoring this
        # check exists to prevent. Say so loudly rather than return a
        # clean-looking empty dict.
        print(
            f"!!! capability check FAILED ({type(exc).__name__}: {exc}) -- "
            "cannot tell which challenges are attemptable; scores from this "
            "run may include challenges that were never runnable here",
            flush=True,
        )
        return {}

    out = {}
    for cid in ids:
        challenge = found.get(cid)
        if challenge is None:
            continue
        why = blocked_reason(challenge)
        if why:
            out[cid] = why
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Score a live innate-os stack on the benchmark.")
    ap.add_argument(
        "--ignore-capabilities",
        action="store_true",
        help="attempt challenges this deployment cannot perform (they will fail)",
    )
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--challenge", action="append", help="repeatable; default = the whole live roster")
    ap.add_argument("--timeout", type=float, default=900.0, help="wall-clock seconds per challenge")
    ap.add_argument(
        "--rosbridge",
        default=DEFAULT_ROSBRIDGE,
        help="where to publish the brief as the user instruction ('' to stay silent)",
    )
    ap.add_argument("--out", type=Path, default=Path("/tmp/bench_live.json"))
    args = ap.parse_args()

    async def go():
        import websockets

        async with websockets.connect(args.url, max_size=None, ping_interval=None, open_timeout=15) as ws:
            roster = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        briefs = {c["id"]: c.get("brief", "") for c in roster.get("challenges", [])}
        ids = args.challenge or list(briefs)

        # What this deployment cannot do, decided BEFORE anything runs, so a
        # missing capability is reported as a missing capability instead of
        # being spent on nineteen timeouts that come back as zeros.
        blocked = _blocked_here(ids) if not args.ignore_capabilities else {}
        if blocked:
            print(f"{len(blocked)} of {len(ids)} challenges cannot be attempted here:")
            for reason in sorted(set(blocked.values())):
                affected = sorted(c for c, r in blocked.items() if r == reason)
                print(f"  {reason}")
                print(f"    {', '.join(affected)}")
            print("  (--ignore-capabilities runs them anyway)")

        print(f"{len(ids)} challenges against {args.url}, {args.timeout:g}s each")
        print(f"instructing via {args.rosbridge or '(nothing -- robot will sit idle)'}")
        if args.rosbridge:
            err = prime()
            print("brain primed (active + skills enabled)" + (f" -- FAILED: {err}" if err else ""))
        print()
        results = await _sweep(
            args.url, ids, args.timeout, args.out, rosbridge=args.rosbridge or None, briefs=briefs, blocked=blocked
        )

        # Blocked challenges are excluded from the denominator. A score out of
        # 38 when 19 were never attempted is a worse lie than no score at all.
        scored = [e for e in results if not e.blocked]
        passed = sum(1 for e in scored if e.passed)
        goals = sum(e.goals_done for e in scored)
        total = sum(e.goals_total for e in scored)
        print(f"\n{passed}/{len(scored)} challenges passed, {goals}/{total} goals")
        if len(scored) != len(results):
            print(f"{len(results) - len(scored)} not attempted (missing capability), excluded from the score")
        # An episode that died in the HARNESS -- a dropped state stream, an id
        # the live roster never had, a websocket exception -- is not evidence
        # about the agent, but it looks identical to a failure in the line
        # above: passed=False, 0 goals. Blocked runs were carved out of the
        # score and these were not, so at minimum they get named.
        broken = [e for e in scored if e.error]
        if broken:
            print(
                f"{len(broken)} of those {len(scored)} failed inside the harness, not the robot "
                f"-- treat the score as {passed}/{len(scored) - len(broken)} until they are re-run:"
            )
            for episode in broken:
                print(f"    {episode.challenge}: {episode.error}")
        print(f"results -> {args.out}")
        return 0

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
