import assert from "node:assert/strict";
import test from "node:test";
import { SlowdownDetector } from "../src/slowdown.ts";

test("warns only after two consecutive slow windows, never across a discontinuity", () => {
  const detector = new SlowdownDetector();
  let now = 0, t = 0;
  const clock = () => ({ t, receivedAt: Math.round(now) });
  const frames = (seconds: number, fps: number, speed = 1, active = true) => {
    let warned = false;
    for (let i = 0; i < fps * seconds; i++) {
      now += 1000 / fps;
      t += speed / fps;
      warned = detector.sample(Math.round(now), clock(), active) || warned;
    }
    return warned;
  };
  // One full 3 s window after begin(); a restart mid-stream shifts the boundary by a frame.
  const window = (fps: number, speed = 1, active = true) => frames(3, fps, speed, active);
  const begin = () => { detector.reset(); detector.sample(Math.round(now), clock(), true); };

  begin();
  assert.equal(window(60) || window(60), false);
  begin();
  assert.equal(window(29.5) || window(29.5), false, "a 30 Hz display with scheduling jitter is healthy");
  begin();
  assert.equal(window(20), false, "one slow window is not enough");
  assert.equal(window(60), false, "recovery clears the streak");
  assert.equal(window(20), false);
  assert.equal(window(20), true, "sustained low fps");
  begin();
  assert.equal(window(60, 0.5), false);
  assert.equal(window(60), false, "a physics dip that recovers never warns");
  assert.equal(window(60, 0.5), false);
  assert.equal(window(60, 0.5), true, "slow physics warns even at 60 fps");

  begin();
  assert.equal(window(20), false);
  now += 30_000; // hidden tab or parked stage: the caller resets around the gap
  begin();
  assert.equal(window(20), false, "a reset restarts the measurement");
  assert.equal(window(20, 1, false), false, "so does an inactive view");
  assert.equal(window(20) || window(20), false);
  assert.equal(window(20), true, "then the same slowness still warns");
  begin();
  assert.equal(window(20), false);
  t = 0.5;
  assert.equal(window(20) || window(20), false, "a clock rollback (world-server restart) restarts it");
  assert.equal(window(20), true);
  t = 0.01;
  begin();
  frames(2, 60);
  t = 0.02;
  frames(1, 60);
  assert.equal(window(20) || window(20), false, "a rollback landing above the window's baseline still restarts it");
  assert.equal(window(20), true);

  begin();
  const frozen = clock();
  for (let i = 0; i < 420; i++) {
    now += 1000 / 60;
    assert.equal(detector.sample(Math.round(now), frozen, true), false, "a stalled world stream is not slow physics");
  }
  assert.equal(detector.sample(Math.round(now), null, true), false, "no state is not evidence of slowdown");
});
