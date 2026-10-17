import assert from "node:assert/strict";
import test from "node:test";
import { startChallengeAndWait } from "../js/agent/challengeRun.js";

function session() {
  const listeners = new Set();
  const active = { id: "jars", attempt_id: "old", state: "running" };
  return {
    starts: 0,
    onChallenge(cb) { listeners.add(cb); cb({ active }); return () => listeners.delete(cb); },
    startChallenge() { this.starts++; return true; },
    emit(active) { for (const cb of [...listeners]) cb({ active }); },
    get listeners() { return listeners.size; },
  };
}

test("retry waits for a new attempt before sending its prompt", async () => {
  const sim = session();
  let ready = false;
  const start = startChallengeAndWait(sim, "jars", new AbortController().signal).then(() => { ready = true; });
  sim.emit({ id: "jars", attempt_id: "old", state: "running" });
  await Promise.resolve();
  assert.equal(ready, false);
  sim.emit({ id: "jars", attempt_id: "new", state: "running" });
  await start;
  assert.equal(ready, true);
  assert.equal(sim.starts, 1);
  assert.equal(sim.listeners, 0);
});

test("disconnect, failed setup, timeout and leaving the page cannot send a prompt", async () => {
  const disconnected = session();
  disconnected.startChallenge = () => false;
  await assert.rejects(startChallengeAndWait(disconnected, "jars", new AbortController().signal), /disconnected/);
  assert.equal(disconnected.listeners, 0);

  const failed = session();
  const rejected = startChallengeAndWait(failed, "jars", new AbortController().signal);
  failed.emit({ id: "jars", attempt_id: "new", state: "failed", reason: "missing prop" });
  await assert.rejects(rejected, /missing prop/);
  assert.equal(failed.listeners, 0);

  const timed = session();
  await assert.rejects(startChallengeAndWait(timed, "jars", new AbortController().signal, 5), /did not become ready/);
  assert.equal(timed.listeners, 0);

  const cancelled = session();
  const controller = new AbortController();
  const pending = startChallengeAndWait(cancelled, "jars", controller.signal);
  controller.abort();
  await assert.rejects(pending, /cancelled/);
  assert.equal(cancelled.listeners, 0);
});
