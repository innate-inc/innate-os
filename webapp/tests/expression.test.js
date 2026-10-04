// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Holds the studio's JS port (js/expression/pipeline.js) to the Python core's
// golden fixture — zero dependencies, plain node:
//   node tests/expression.test.js
// The studio previews what the robot will do only while the two agree, so every
// stage is checked: expansion, plan, interpolation, liveliness, the basis and its
// rate limit, the PRNG, randomised variants, osc gesture words, idle breathing,
// the presets, the frozen planner prompt, and the checker's error strings (they
// are the planner's repair hints).
// Regenerate the fixture: cd expressive && uv run mars-express golden

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  ACTUATOR_KEYS,
  CHANNELS,
  breathing,
  checkRecipe,
  expandRecipe,
  limitFrames,
  liveliness,
  mulberry32,
  planFrames,
  synthesize,
  toPlan,
  variants,
} from "../js/expression/pipeline.js";
import { SYSTEM_PROMPT } from "../js/expression/planner.js";
import { DEFAULT_PRESET, PRESETS, nearestPreset } from "../js/expression/presets.js";

const repo = new URL("../../", import.meta.url);
const golden = JSON.parse(readFileSync(new URL("expressive/fixtures/golden.json", repo), "utf8"));
const core = new URL("ros2_ws/src/brain/brain_client/brain_client/expressive/", repo);
const coreBasis = JSON.parse(readFileSync(new URL("basis.json", core), "utf8"));

const EXPANSION_TOL = 1e-6;
const GENERATED_TOL = 1e-4;

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** Largest |a - b| over two equally shaped number grids. @param {number[][]} a @param {number[][]} b @param {string} what */
function maxDiff(a, b, what) {
  assert.equal(a.length, b.length, `${what}: ${a.length} rows vs ${b.length}`);
  let worst = 0;
  a.forEach((row, i) => {
    assert.equal(row.length, b[i].length, `${what}: row ${i} width`);
    row.forEach((v, j) => (worst = Math.max(worst, Math.abs(v - b[i][j]))));
  });
  return worst;
}

/** @param {{ duration: number, keys: Record<string, number>[] }} plan */
const planGrid = (plan) => plan.keys.map((k) => ["t", ...CHANNELS].map((c) => k[c]));

/** @param {any} js @param {any} py @param {number} tol @param {string} what */
function assertPlan(js, py, tol, what) {
  assert.ok(Math.abs(js.duration - py.duration) < 1e-9, `${what}: duration ${js.duration} vs ${py.duration}`);
  const d = maxDiff(planGrid(js), planGrid(py), what);
  assert.ok(d < tol, `${what}: max diff ${d}`);
}

test("the fixture was generated from the current basis.json", () => {
  assert.equal(golden.basis_version, coreBasis.version, "regenerate it: cd expressive && uv run mars-express golden");
});

test("mulberry32 is bit-identical", () => {
  const rand = mulberry32(0);
  assert.deepEqual(
    golden.mulberry32_seed0.map(() => rand()),
    golden.mulberry32_seed0,
  );
});

for (const [n, c] of golden.cases.entries()) {
  const label = `case ${n} "${c.recipe.slice(0, 40)}…"`;
  test(`${label}: expansion, plan, interpolation`, () => {
    const frames = expandRecipe(c.recipe);
    assert.ok(maxDiff(frames, c.frames, "frames") < EXPANSION_TOL);
    const plan = toPlan(frames);
    assertPlan(plan, c.plan, EXPANSION_TOL, "plan");
    assert.ok(maxDiff(planFrames(plan), c.plan_frames, "plan_frames") < EXPANSION_TOL);
  });
  test(`${label}: liveliness and actuators`, () => {
    const motion = liveliness(c.plan_frames, c.seed);
    assert.ok(maxDiff(motion, c.motion, "motion") < GENERATED_TOL);
    const keys = Object.keys(c.actuators[0]);
    const actuators = c.motion.map((/** @type {number[]} */ row) => synthesize(coreBasis, [...row, 0]));
    const grid = (/** @type {Record<string, number>[]} */ poses) => poses.map((p) => keys.map((k) => p[k]));
    assert.ok(maxDiff(grid(actuators), grid(c.actuators), "actuators") < GENERATED_TOL);
    const limited = limitFrames(coreBasis, actuators, 1 / golden.fps).map((p) => ACTUATOR_KEYS.map((k) => p[k]));
    assert.ok(maxDiff(limited, c.limited, "limited") < GENERATED_TOL);
  });
}

test("osc gesture words expand like their letters", () => {
  for (const { recipe, letters, same } of golden.aliases) {
    assert.equal(maxDiff(expandRecipe(recipe), expandRecipe(letters), recipe) === 0, same, recipe);
  }
});

test("variants draw amplitude, tempo and jitter in the core's order", () => {
  const { recipe, n, seed, plans } = golden.variants;
  variants(recipe, n, seed).forEach((plan, i) => assertPlan(plan, plans[i], EXPANSION_TOL, `variant ${i}`));
});

test("checker errors match the core's strings", () => {
  for (const { recipe, error } of golden.errors) assert.equal(checkRecipe(recipe), error, JSON.stringify(recipe));
});

test("idle breathing samples match", () => {
  const ours = golden.breathing.map((/** @type {{ t: number }} */ s) => breathing(s.t).slice(0, 8));
  assert.ok(
    maxDiff(
      ours,
      golden.breathing.map((/** @type {{ row: number[] }} */ s) => s.row),
      "breathing",
    ) < EXPANSION_TOL,
  );
});

test("presets mirror the core's, keyword matching included", () => {
  assert.deepEqual(PRESETS, golden.presets);
  assert.equal(DEFAULT_PRESET, golden.default_preset);
  for (const p of PRESETS) assert.equal(checkRecipe(p.recipe), null, p.name);
  assert.equal(nearestPreset("I'm so tired, time for bed").name, "sleepy");
  assert.equal(nearestPreset("a toaster about to pop").name, DEFAULT_PRESET);
});

// The prompt is one triple-quoted literal in prompt.py, read as text.
test("the planner prompt is the core's frozen SYSTEM prompt", () => {
  const python = readFileSync(new URL("prompt.py", core), "utf8");
  assert.equal(SYSTEM_PROMPT, /SYSTEM = """([\s\S]*?)"""/.exec(python)?.[1]);
});

console.log(`\n${passed} passed`);
