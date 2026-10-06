// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Blind VLM judges for a motion. The judge sees a few key frames of the clip,
// in time order, from the person's own eyes, and is never told the prompt: it
// spreads probability over a fixed label vocabulary, so a clip scores on what
// an uninvolved observer reads, not on priming. The pairwise judge does name a
// target ("which reads more X?") because comparisons are what VLMs rate
// reliably; A/B order is coin-flipped per call to cancel position bias.
//
// Gemini generateContent straight from the browser (bench tooling; the key
// stays in this browser's localStorage).

const BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models";
const SETTINGS_KEY = "innate.expression.gemini";
const API_KEY_KEY = "innate.geminiApiKey";
const DEFAULT_MODEL = "gemini-3.6-flash";

// Fixed so scores stay comparable across sessions: the presets' feelings plus
// the readings a failed motion collapses into (confused, neutral, bored).
export const JUDGE_LABELS = [
  "curious",
  "excited",
  "happy",
  "proud",
  "affectionate",
  "playful",
  "calm",
  "sleepy",
  "sad",
  "ashamed",
  "scared",
  "startled",
  "angry",
  "disgusted",
  "confused",
  "bored",
  "neutral",
];

/** @typedef {{ apiKey: string, plannerModel: string, judgeModel: string, judges: number }} GeminiConfig */
/** @typedef {{ dataUrl: string, t: number }} KeyFrame */
/** @typedef {{ probabilities: Record<string, number>, cues: string[], impression: string }} Reading */
/** @typedef {{ mean: Record<string, number>, runs: Reading[], errors: string[] }} PanelResult */
/** @typedef {{ winner: "A" | "B", margin: "clear" | "slight", reason: string }} Verdict */

/** @returns {GeminiConfig} */
export function loadGeminiConfig() {
  /** @type {any} */
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  } catch {
    stored = {};
  }
  return {
    apiKey: localStorage.getItem(API_KEY_KEY) || "",
    plannerModel: typeof stored.plannerModel === "string" && stored.plannerModel ? stored.plannerModel : DEFAULT_MODEL,
    judgeModel: typeof stored.judgeModel === "string" && stored.judgeModel ? stored.judgeModel : DEFAULT_MODEL,
    judges: Math.min(5, Math.max(1, Number(stored.judges) || 3)),
  };
}

/** @param {GeminiConfig} cfg */
export function saveGeminiConfig(cfg) {
  localStorage.setItem(API_KEY_KEY, cfg.apiKey);
  localStorage.setItem(
    SETTINGS_KEY,
    JSON.stringify({ plannerModel: cfg.plannerModel, judgeModel: cfg.judgeModel, judges: cfg.judges }),
  );
}

const ROBOT_CONTEXT =
  "The images are key frames, in time order, of one short motion by a small non-humanoid robot, " +
  "seen through the eyes of a person standing about a metre in front of it. The robot has a dark " +
  "wheeled base, one arm with an orange gripper claw (its only limb), and a flat camera head that can " +
  "only tilt up or down. Between frames it may have turned or rolled a little.";

/** @param {KeyFrame[]} frames */
function frameParts(frames) {
  return frames.flatMap((f, i) => [
    { text: `frame ${i + 1} (t = ${f.t.toFixed(1)} s):` },
    { inlineData: { mimeType: "image/jpeg", data: f.dataUrl.replace(/^data:image\/\w+;base64,/, "") } },
  ]);
}

/** @param {GeminiConfig} cfg @param {object[]} parts @param {object} schema @param {number} temperature */
async function generate(cfg, parts, schema, temperature) {
  const res = await fetch(`${BASE_URL}/${encodeURIComponent(cfg.judgeModel)}:generateContent`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-goog-api-key": cfg.apiKey },
    body: JSON.stringify({
      contents: [{ role: "user", parts }],
      generationConfig: { temperature, responseMimeType: "application/json", responseSchema: schema },
    }),
    signal: AbortSignal.timeout(90_000),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const data = await res.json();
  const text = (data?.candidates?.[0]?.content?.parts ?? []).map((/** @type {any} */ p) => p?.text || "").join("");
  if (!text) throw new Error("empty judge response");
  return JSON.parse(text);
}

const READING_SCHEMA = {
  type: "OBJECT",
  properties: {
    probabilities: {
      type: "OBJECT",
      properties: Object.fromEntries(JUDGE_LABELS.map((l) => [l, { type: "NUMBER" }])),
      required: JUDGE_LABELS,
    },
    cues: { type: "ARRAY", items: { type: "STRING" } },
    impression: { type: "STRING" },
  },
  required: ["probabilities", "cues", "impression"],
};

/** One blind reading; the prompt never names an intended feeling. @param {KeyFrame[]} frames @param {GeminiConfig} cfg @returns {Promise<Reading>} */
export async function judgeBlind(frames, cfg) {
  const prompt =
    `${ROBOT_CONTEXT}\n\n` +
    "Judge ONLY the body language across the frames — how the arm's posture, the gripper, the head " +
    "tilt and the base's facing change over time. Do not assume any particular feeling was intended.\n\n" +
    "Estimate the probability that a typical person watching this motion would describe the robot " +
    `with each of these labels: ${JUDGE_LABELS.join(", ")}. Probabilities must sum to 1. ` +
    'Also give "cues": 2-4 short physical observations that drove your reading, and "impression": ' +
    "one sentence on what the motion communicates.";
  const raw = await generate(cfg, [{ text: prompt }, ...frameParts(frames)], READING_SCHEMA, 0.7);
  return {
    probabilities: normalize(raw?.probabilities),
    cues: Array.isArray(raw?.cues) ? raw.cues.map(String).slice(0, 6) : [],
    impression: String(raw?.impression || ""),
  };
}

/**
 * Independent blind readings, averaged; per-judge failures land in `errors`
 * (one flaky call must not sink the panel).
 * @param {KeyFrame[]} frames @param {GeminiConfig} cfg @param {(done: number) => void} [onProgress]
 * @returns {Promise<PanelResult>}
 */
export async function judgePanel(frames, cfg, onProgress) {
  /** @type {Reading[]} */
  const runs = [];
  /** @type {string[]} */
  const errors = [];
  let done = 0;
  await Promise.all(
    Array.from({ length: cfg.judges }, async () => {
      try {
        runs.push(await judgeBlind(frames, cfg));
      } catch (err) {
        errors.push(err instanceof Error ? err.message : String(err));
      }
      onProgress?.(++done);
    }),
  );
  const mean = Object.fromEntries(
    JUDGE_LABELS.map((label) => [
      label,
      runs.length ? runs.reduce((sum, r) => sum + (r.probabilities[label] ?? 0), 0) / runs.length : 0,
    ]),
  );
  return { mean, runs, errors };
}

const VERDICT_SCHEMA = {
  type: "OBJECT",
  properties: {
    winner: { type: "STRING", enum: ["first", "second"] },
    margin: { type: "STRING", enum: ["clear", "slight"] },
    reason: { type: "STRING" },
  },
  required: ["winner", "margin", "reason"],
};

/**
 * Which of two motions reads more as `target`; presentation order is
 * coin-flipped and un-flipped in the verdict.
 * @param {KeyFrame[]} framesA @param {KeyFrame[]} framesB @param {string} target @param {GeminiConfig} cfg
 * @returns {Promise<Verdict>}
 */
export async function judgePair(framesA, framesB, target, cfg) {
  const flipped = Math.random() < 0.5;
  const [first, second] = flipped ? [framesB, framesA] : [framesA, framesB];
  const prompt =
    `${ROBOT_CONTEXT}\n\n` +
    "You will see two different motions: first the frames of motion ONE, then the frames of motion " +
    `TWO. Which motion would a typical person more likely describe as "${target}"? Judge only the ` +
    "body language. Answer with the winner, whether the difference is clear or slight, and one " +
    "sentence of reasoning about the movement.";
  const raw = await generate(
    cfg,
    [{ text: prompt }, { text: "Motion ONE:" }, ...frameParts(first), { text: "Motion TWO:" }, ...frameParts(second)],
    VERDICT_SCHEMA,
    0.3,
  );
  const pickedFirst = raw?.winner === "first";
  return {
    winner: pickedFirst !== flipped ? "A" : "B",
    margin: raw?.margin === "clear" ? "clear" : "slight",
    reason: String(raw?.reason || ""),
  };
}

/**
 * Coerce a raw probabilities object onto the label set: non-finite/negative
 * become 0, then normalize (uniform when the judge returned nothing).
 * @param {any} raw @returns {Record<string, number>}
 */
export function normalize(raw) {
  /** @type {Record<string, number>} */
  const out = {};
  let sum = 0;
  for (const label of JUDGE_LABELS) {
    const v = Number(raw?.[label]);
    out[label] = Number.isFinite(v) && v > 0 ? v : 0;
    sum += out[label];
  }
  for (const label of JUDGE_LABELS) out[label] = sum > 0 ? out[label] / sum : 1 / JUDGE_LABELS.length;
  return out;
}
