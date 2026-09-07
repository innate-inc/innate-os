import assert from "node:assert/strict";
import test from "node:test";
import { SlowdownDetector } from "../src/slowdown.ts";

test("warns only after 6 s of uninterrupted slow frames or slow simulation", () => {
  const detector = new SlowdownDetector();
  let now = 0, t = 0;
  const run = (seconds: number, fps: number, speed = 1, active = true) => {
    let warned = false;
    for (let i = 0; i < fps * seconds; i++) {
      now += 1000 / fps;
      t += speed / fps;
      warned = detector.sample(now, { t, receivedAt: now }, active) || warned;
    }
    return warned;
  };
  const fresh = (fps: number, speed = 1) => { now += 2000; return run(7, fps, speed); };

  assert.equal(fresh(60), false);
  assert.equal(fresh(29.5), false, "a 30 Hz display with scheduling jitter is healthy");
  assert.equal(fresh(20), true, "sustained low fps");
  assert.equal(fresh(60, 0.5), true, "slow physics warns even at 60 fps");

  now += 2000;
  assert.equal(run(4, 20), false, "too early");
  now += 2000;
  assert.equal(run(4, 20), false, "a frame gap (hidden tab, parked stage) restarts the window");
  assert.equal(run(4, 20, 1, false), false, "an inactive view restarts the window");
  assert.equal(run(4, 20), false);
  t = 0;
  assert.equal(run(4, 20), false, "a clock rollback (world-server restart) restarts the window");
  assert.equal(run(3, 20), true, "then the same slowness still warns");

  now += 2000;
  const frozen = { t, receivedAt: now };
  for (let i = 0; i < 420; i++) {
    now += 1000 / 60;
    assert.equal(detector.sample(now, frozen, true), false, "a stalled world stream is not slow physics");
  }
  assert.equal(detector.sample(now, null, true), false, "no state is not evidence of slowdown");
});
