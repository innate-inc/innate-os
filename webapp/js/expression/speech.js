// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// How the robot turns one spoken reply into performed sentences, ported from
// brain_client: the sentence splitter (transport/chat.py _split_sentences), the
// emote-tag and tool-narration scrubs (brain/context.py split_emotes, emote_spans,
// split_tool_narration), the reply streamer's cues (chat.py SpeechStreamer) and
// the planner prompt for one sentence (expressive/prompt.py speech_prompt). Held
// to the Python by tests/expression.test.js against expressive/fixtures/speech_golden.json.

// Python's \s and str.split(): JS's \s lacks \x1c-\x1f and \x85 and adds \ufeff.
const WS = "[\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const SENTENCE_END = new RegExp(`(?<=[.!?…])${WS}+`, "gu");
const EMOTE = new RegExp(
  `<${WS}*emote${WS}*>([^<\\]\\n]{0,80}?)(?:<${WS}*\\/?${WS}*emote${WS}*\\/?${WS}*>|(?=[<\\n])|$)` +
    `|\\[emote:([^<\\]\\n]{0,80}?)(?:\\]|(?=[<\\n])|$)`,
  "giu",
);
const STRAY_EMOTE = new RegExp(`<${WS}*\\/?${WS}*emote[^>]*>`, "giu");
const SPACE_BEFORE_PUNCT = new RegExp(`${WS}+([,.!?;:…])`, "gu");
const WS_RUN = new RegExp(`${WS}+`, "u");
const EDGE_WS = new RegExp(`^${WS}+|${WS}+$`, "gu");
const TRAILING_WS = new RegExp(`${WS}+$`, "u");
// Python's \b is Unicode: a letter or digit right after "tool" is no boundary.
const TOOL_NARRATION = /Calling tool(?![\p{L}\p{N}_])/u;
const QUOTE_LIMIT = 140;

// The robot's fit of speech time on 232 sim TTS clips (expressive/director.py).
const SPEECH_BASE_S = 0.46;
const SPEECH_PER_CHAR_S = 0.055;

/**
 * One spoken sentence as the streamer hands it to the body: its emote tags, its context, the tags
 * written after it when it ends the reply (`closing`), and the prompt its motion is planned from.
 * @typedef {{ text: string, emotes: string[], before: string | null, heard: string | null,
 *             prompt: string, closing: string[] }} Beat
 */

/** Python's str.split(). @param {string} text */
const words = (text) => text.split(WS_RUN).filter(Boolean);

/** Python's str.strip(). @param {string} text */
const strip = (text) => text.replace(EDGE_WS, "");

/**
 * Where the emote tags are (UTF-16 offsets); one still waiting for its closer runs to the end.
 * @param {string} text
 */
export function emoteSpans(text) {
  return [...text.matchAll(EMOTE)].map(
    (m) => /** @type {[number, number]} */ ([m.index, m.index + m[0].length]),
  );
}

/**
 * The text without its emote tags, and the tags' prompts in order.
 * @param {string} text @returns {[string, string[]]}
 */
export function splitEmotes(text) {
  const prompts = [...text.matchAll(EMOTE)].map((m) => words(m[1] || m[2] || "").join(" "));
  const stripped = text.replace(EMOTE, " ").replace(STRAY_EMOTE, " ");
  if (stripped === text) return [text, []];
  const clean = words(stripped).join(" ").replace(SPACE_BEFORE_PUNCT, "$1");
  return [clean, prompts.filter(Boolean)];
}

/**
 * Leaked tool-call narration cut off ("Calling tool ..." to the end), and whether there was any.
 * @param {string} text @returns {[string, boolean]}
 */
export function splitToolNarration(text) {
  const at = text.search(TOOL_NARRATION);
  return at < 0 ? [text, false] : [text.slice(0, at).replace(TRAILING_WS, ""), true];
}

/**
 * Complete sentences and the unfinished rest; no boundary inside an emote tag counts.
 * @param {string} text @returns {[string[], string]}
 */
export function splitSentences(text) {
  const tags = emoteSpans(text);
  /** @type {string[]} */
  const sentences = [];
  let start = 0;
  for (const boundary of text.matchAll(SENTENCE_END)) {
    if (tags.some(([a, b]) => a <= boundary.index && boundary.index < b)) continue;
    sentences.push(text.slice(start, boundary.index));
    start = boundary.index + boundary[0].length;
  }
  return [sentences, text.slice(start)];
}

/** @param {string} text */
function quote(text) {
  const flat = words(text.replaceAll('"', "'")).join(" ");
  const chars = Array.from(flat);
  if (chars.length <= QUOTE_LIMIT) return flat;
  const head = chars.slice(0, QUOTE_LIMIT).join("");
  const cut = head.lastIndexOf(" ");
  return `${cut < 0 ? head : head.slice(0, cut)}…`;
}

/**
 * The planner prompt that performs one spoken sentence (FROZEN with the planner prompt).
 * @param {string} sentence
 * @param {string | null} [heard] what the person last said
 * @param {string | null} [before] the robot's previous sentence in the same reply
 */
export function speechPrompt(sentence, heard = null, before = null) {
  let prompt = `You say: "${quote(sentence)}"`;
  if (before) prompt += ` right after: "${quote(before)}"`;
  if (heard) prompt += ` to someone who said: "${quote(heard)}"`;
  return prompt;
}

/**
 * A whole reply as the robot's reply streamer cues it: one beat per spoken sentence, a tag-only
 * fragment's tags waiting for the next spoken sentence, and nothing after leaked tool narration.
 * A beat's last tag replaces its speech prompt; `heard` only reaches the first sentence. Tags left
 * after the last sentence close it: handed over whole, the reply closes it before its audio starts,
 * so they replace its prompt (the driver's `_close_cue`).
 * @param {string} reply @param {string} [heard] @returns {Beat[]}
 */
export function replyBeats(reply, heard = "") {
  const [sentences, tail] = splitSentences(reply);
  /** @type {Beat[]} */
  const beats = [];
  /** @type {string[]} */
  let held = [];
  let muted = false;
  for (const raw of [...sentences, tail]) {
    if (muted) break;
    const [clean, emotes] = splitEmotes(raw);
    held.push(...emotes);
    const [text, truncated] = splitToolNarration(strip(clean));
    muted = truncated;
    if (!/[a-zA-Z0-9]/.test(text)) continue;
    const before = beats.at(-1)?.text ?? null;
    const opens = beats.length === 0;
    const prompt = held.at(-1) ?? speechPrompt(text, opens ? heard : null, before);
    beats.push({ text, emotes: held, before, heard: opens && heard ? heard : null, prompt, closing: [] });
    held = [];
  }
  const last = beats.at(-1);
  if (last && !muted && held.length) {
    last.closing = held;
    last.prompt = /** @type {string} */ (held.at(-1));
  }
  return beats;
}

/** Seconds the robot's voice takes for `sentence`, for timing a performance with no voice. @param {string} sentence */
export const speechSeconds = (sentence) =>
  sentence ? SPEECH_BASE_S + SPEECH_PER_CHAR_S * Array.from(sentence).length : 0;
