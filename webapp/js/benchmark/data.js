// @ts-check
// Pure helpers for the sim-only Benchmark panel: parsing the benchmark
// service's payloads and shaping them for display. No DOM, no fetch — this
// module is exercised by tests/benchmark.test.js.

/** Category display order; anything unknown sorts after these. */
export const CATEGORIES = ["find_object", "find_location", "instruction", "open_ended", "honesty"];

/**
 * @typedef {{task: string, category: string, score: number, reason: string,
 *            frame: number | null, room: string | null, latency_sec: number,
 *            cached: boolean, explanation: string, error: string}} TaskRow
 */

/**
 * Mean score overall and per category for one model's rows.
 * @param {TaskRow[]} rows
 * @returns {{overall: number, byCategory: Map<string, number>, meanLatency: number | null}}
 */
export function summarize(rows) {
  /** @type {Map<string, number[]>} */
  const buckets = new Map();
  for (const row of rows) {
    const list = buckets.get(row.category) ?? [];
    list.push(row.score);
    buckets.set(row.category, list);
  }
  const byCategory = new Map(
    [...buckets.entries()]
      .sort((a, b) => categoryRank(a[0]) - categoryRank(b[0]))
      .map(([cat, scores]) => [cat, mean(scores)]),
  );
  const latencies = rows.map((r) => r.latency_sec).filter((v) => v > 0);
  return {
    overall: rows.length ? mean(rows.map((r) => r.score)) : 0,
    byCategory,
    meanLatency: latencies.length ? mean(latencies) : null,
  };
}

/** @param {string} category */
export function categoryRank(category) {
  const i = CATEGORIES.indexOf(category);
  return i === -1 ? CATEGORIES.length : i;
}

/** @param {number[]} values */
function mean(values) {
  return values.reduce((a, b) => a + b, 0) / values.length;
}

/** CSS suffix for a 0..1 score: ok / mid / bad. @param {number} score */
export function scoreClass(score) {
  if (score >= 0.99) return "ok";
  if (score >= 0.49) return "mid";
  return "bad";
}

/**
 * "20260807-031438-oracle" -> {stamp: "2026-08-07 03:14", mode: "oracle"}.
 * A name that doesn't match the runner's pattern passes through as the mode.
 * @param {string} name
 */
export function runLabel(name) {
  const m = name.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}-(.+)$/);
  if (!m) return { stamp: "", mode: name };
  return { stamp: `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`, mode: m[6] };
}

/**
 * Normalize /api/state. Missing fields become empty; a dataset needs a
 * capture block to count (a half-written directory does not).
 * @param {any} payload
 * @returns {{geminiKey: boolean, datasets: any[], results: any[], job: any | null}}
 */
export function parseState(payload) {
  return {
    geminiKey: !!payload?.gemini_key,
    datasets: Array.isArray(payload?.datasets)
      ? payload.datasets.filter((/** @type {any} */ d) => d && d.capture)
      : [],
    results: Array.isArray(payload?.results) ? payload.results : [],
    job: payload?.job ?? null,
  };
}

/** @param {number | null} seconds */
export function fmtLatency(seconds) {
  return seconds == null ? "—" : `${seconds.toFixed(1)}s`;
}

/** @param {number} value 0..1 */
export function fmtScore(value) {
  return value.toFixed(2);
}
