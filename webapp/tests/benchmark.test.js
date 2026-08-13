// Run: node tests/benchmark.test.js
import assert from "node:assert/strict";
import { categoryRank, fmtLatency, parseState, runLabel, scoreClass, summarize } from "../js/benchmark/data.js";

let passed = 0;
function test(name, fn) {
  fn();
  console.log(`ok - ${name}`);
  passed += 1;
}

const row = (category, score, latency = 0) => ({
  task: "t",
  category,
  score,
  reason: "",
  frame: null,
  room: null,
  latency_sec: latency,
  cached: false,
  explanation: "",
  error: "",
});

test("summarize averages overall and per category in display order", () => {
  const s = summarize([row("honesty", 1), row("find_object", 1), row("find_object", 0), row("open_ended", 0.5)]);
  assert.equal(s.overall, 0.625);
  assert.deepEqual([...s.byCategory.keys()], ["find_object", "open_ended", "honesty"]);
  assert.equal(s.byCategory.get("find_object"), 0.5);
});

test("summarize latency ignores zero (fake runs) and empty input scores 0", () => {
  assert.equal(summarize([row("a", 1), row("a", 1, 2.0), row("a", 1, 4.0)]).meanLatency, 3.0);
  assert.equal(summarize([row("a", 1)]).meanLatency, null);
  assert.equal(summarize([]).overall, 0);
});

test("unknown categories sort after the known ones", () => {
  assert.ok(categoryRank("weird_new_thing") > categoryRank("honesty"));
});

test("scoreClass buckets", () => {
  assert.equal(scoreClass(1), "ok");
  assert.equal(scoreClass(0.5), "mid");
  assert.equal(scoreClass(0), "bad");
});

test("runLabel parses runner directory names and passes odd names through", () => {
  assert.deepEqual(runLabel("20260807-031438-oracle"), { stamp: "2026-08-07 03:14", mode: "oracle" });
  assert.deepEqual(runLabel("20260807-120000-cached"), { stamp: "2026-08-07 12:00", mode: "cached" });
  assert.deepEqual(runLabel("adhoc"), { stamp: "", mode: "adhoc" });
});

test("parseState tolerates junk and drops half-written datasets", () => {
  const state = parseState({
    gemini_key: 1,
    datasets: [{ name: "ok", capture: { frames: 3 } }, { name: "torn" }, null],
    results: null,
    job: undefined,
  });
  assert.equal(state.geminiKey, true);
  assert.deepEqual(state.datasets.map((d) => d.name), ["ok"]);
  assert.deepEqual(state.results, []);
  assert.equal(state.job, null);
  assert.deepEqual(parseState(null).datasets, []);
});

test("fmtLatency", () => {
  assert.equal(fmtLatency(null), "—");
  assert.equal(fmtLatency(1.234), "1.2s");
});

console.log(`\n${passed} passed`);
