// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Blind VLM judges for the Expression Studio. The absolute judge is never
// told the target emotion — it distributes probability over a fixed label
// vocabulary from the renders alone, so a pose scores on what an uninvolved
// observer would read, not on priming. The pairwise judge does name the
// target ("which looks more X?") because comparisons are what VLMs rate
// reliably; A/B order is coin-flipped per call to kill position bias.
//
// Calls Gemini's generateContent directly (the brain's dev path —
// generativelanguage.googleapis.com with an API key), straight from the
// browser: this is bench tooling, the key stays in localStorage.

const BASE_URL = "https://generativelanguage.googleapis.com";
const DEFAULT_MODEL = "gemini-3.6-flash"; // the brain's default model
const CONFIG_KEY = "innate.expression.judge";
const API_KEY_KEY = "innate.geminiApiKey";

// The perceptual vocabulary — a superset of the preset emotions plus the
// readings poses collapse into when they fail (confused, aggressive,
// neutral). Fixed so scores stay comparable across sessions.
export const JUDGE_LABELS = [
  "curious",
  "excited",
  "confident",
  "affectionate",
  "fearful",
  "sad",
  "suspicious",
  "confused",
  "aggressive",
  "neutral",
];

/** @typedef {{ apiKey: string, model: string, judges: number }} JudgeConfig */
/** @typedef {{ probabilities: Record<string, number>, cues: string[], impression: string }} Reading */
/** @typedef {{ mean: Record<string, number>, runs: Reading[], errors: string[] }} PanelResult */

/** @returns {JudgeConfig} */
export function loadJudgeConfig() {
  /** @type {any} */
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(CONFIG_KEY) || "{}");
  } catch {
    stored = {};
  }
  return {
    apiKey: localStorage.getItem(API_KEY_KEY) || "",
    model: typeof stored.model === "string" && stored.model ? stored.model : DEFAULT_MODEL,
    judges: Math.min(5, Math.max(1, Number(stored.judges) || 3)),
  };
}

/** @param {JudgeConfig} cfg */
export function saveJudgeConfig(cfg) {
  localStorage.setItem(API_KEY_KEY, cfg.apiKey);
  localStorage.setItem(CONFIG_KEY, JSON.stringify({ model: cfg.model, judges: cfg.judges }));
}

const ROBOT_CONTEXT =
  "The images are renders of one single moment, from different viewpoints, of a small non-humanoid " +
  "robot: a dark wheeled base, one small arm with an orange gripper claw, and a flat head unit on a " +
  "neck that can only pitch up or down. The translucent blue figure is a person standing nearby; the " +
  "robot may be reacting to them. One view is from the person's own eyes.";

/** @param {{ dataUrl: string }[]} images */
function imageParts(images) {
  return images.map((img) => ({
    inlineData: { mimeType: "image/jpeg", data: img.dataUrl.replace(/^data:image\/\w+;base64,/, "") },
  }));
}

/**
 * @param {JudgeConfig} cfg @param {object[]} parts @param {object} schema
 * @param {number} temperature @returns {Promise<any>}
 */
async function generate(cfg, parts, schema, temperature) {
  const res = await fetch(`${BASE_URL}/v1beta/models/${cfg.model}:generateContent`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-goog-api-key": cfg.apiKey },
    body: JSON.stringify({
      contents: [{ role: "user", parts }],
      generationConfig: { temperature, responseMimeType: "application/json", responseSchema: schema },
    }),
    signal: AbortSignal.timeout(90_000),
  });
  if (!res.ok) {
    const detail = (await res.text()).slice(0, 200);
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }
  const data = await res.json();
  const text = (data?.candidates?.[0]?.content?.parts ?? [])
    .map((/** @type {any} */ p) => p?.text || "")
    .join("");
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

/**
 * One blind reading of a pose. The prompt never names a target emotion.
 * @param {{ dataUrl: string }[]} images @param {JudgeConfig} cfg @returns {Promise<Reading>}
 */
export async function judgeBlind(images, cfg) {
  const prompt =
    `${ROBOT_CONTEXT}\n\n` +
    "Judge ONLY the robot's body language: where it sits and faces relative to the person, its arm " +
    "posture, gripper, and head pitch. Do not assume any intended emotion was designed in.\n\n" +
    "Estimate the probability that a typical person seeing this robot would describe its attitude " +
    `with each of these labels: ${JUDGE_LABELS.join(", ")}. Probabilities must sum to 1. ` +
    'Also give "cues": 2-4 short physical observations that drove your reading, and "impression": ' +
    "one sentence saying what the robot's stance communicates.";
  const raw = await generate(cfg, [{ text: prompt }, ...imageParts(images)], READING_SCHEMA, 0.7);
  return {
    probabilities: normalize(raw?.probabilities),
    cues: Array.isArray(raw?.cues) ? raw.cues.map(String).slice(0, 6) : [],
    impression: String(raw?.impression || ""),
  };
}

/**
 * A panel of independent blind readings, averaged. Per-judge failures land in
 * `errors` — one flaky call must not sink the panel.
 * @param {{ dataUrl: string }[]} images @param {JudgeConfig} cfg
 * @param {(done: number) => void} [onProgress] @returns {Promise<PanelResult>}
 */
export async function judgePanel(images, cfg, onProgress) {
  /** @type {Reading[]} */
  const runs = [];
  /** @type {string[]} */
  const errors = [];
  let done = 0;
  await Promise.all(
    Array.from({ length: cfg.judges }, async () => {
      try {
        runs.push(await judgeBlind(images, cfg));
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

/** @typedef {{ winner: "A" | "B", margin: "clear" | "slight", reason: string }} Verdict */

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
 * One pairwise comparison for a named target. Presentation order is
 * coin-flipped and un-flipped in the verdict, so "first is better" bias
 * cancels over a panel.
 * @param {{ dataUrl: string }[]} imagesA @param {{ dataUrl: string }[]} imagesB
 * @param {string} target @param {JudgeConfig} cfg @returns {Promise<Verdict>}
 */
export async function judgePair(imagesA, imagesB, target, cfg) {
  const flipped = Math.random() < 0.5;
  const [first, second] = flipped ? [imagesB, imagesA] : [imagesA, imagesB];
  const prompt =
    `${ROBOT_CONTEXT}\n\n` +
    "You will see the robot in two different poses: first every view of pose ONE, then every view of " +
    `pose TWO. Which pose would a typical person more likely describe as "${target}"? Judge only the ` +
    "body language. Answer with the winner, whether the difference is clear or slight, and one " +
    "sentence of reasoning based on posture.";
  const raw = await generate(
    cfg,
    [
      { text: prompt },
      { text: "Pose ONE:" },
      ...imageParts(first),
      { text: "Pose TWO:" },
      ...imageParts(second),
    ],
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
 * become 0, then normalize to sum 1 (uniform if the judge returned nothing).
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
  for (const label of JUDGE_LABELS) {
    out[label] = sum > 0 ? out[label] / sum : 1 / JUDGE_LABELS.length;
  }
  return out;
}
