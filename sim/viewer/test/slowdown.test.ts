import assert from "node:assert/strict";
import test from "node:test";
import { SlowdownDetector } from "../src/slowdown.ts";

test("warn only on sustained slow rendering or simulation, excluding inactive/reset periods", () => {
  const detector = new SlowdownDetector();
  let now = 0, t = 0;
  const window = (fps = 60, speed = 1, active = true) => {
    let warned = false;
    for (let i = 0; i < fps * 3; i++) {
      now += 1000 / fps;
      t += speed / fps;
      warned = detector.sample(Math.round(now), { t, receivedAt: Math.round(now) }, active) || warned;
    }
    return warned;
  };
  const begin = () => { detector.reset(); detector.sample(Math.round(now), { t, receivedAt: Math.round(now) }, true); };
  begin();
  assert.equal(window(29.5), false);
  assert.equal(window(29.5), false, "30 Hz cap with scheduling jitter is not a rendering failure");
  begin();
  assert.equal(window(), false);
  assert.equal(window(20), false, "one poor window is insufficient");
  assert.equal(window(), false, "recovery breaks the streak");
  assert.equal(window(20), false);
  assert.equal(window(20), true);
  begin();
  assert.equal(window(60, .5), false);
  assert.equal(window(60, .5), true, "slow physics warns even at 60fps");
  begin();
  assert.equal(window(20), false);
  assert.equal(window(20, .5, false), false, "loading/hidden views reset the detector");
  assert.equal(window(), false);
  begin();
  assert.equal(window(20), false);
  detector.reset();
  assert.equal(window(20), false, "environment load discards the previous bad window");
  assert.equal(detector.sample(now, null, true), false, "no state is not evidence of slowdown");
  begin();
  assert.equal(window(20), false);
  t = -100;
  assert.equal(window(20), false, "clock rollback resets the window");
  begin();
  const frozen = { t, receivedAt: Math.round(now) };
  for (let i = 0; i < 500; i++) {
    now += 17;
    assert.equal(detector.sample(now, frozen, true), false, "a stream stall is not slow physics");
  }
});
