// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The core's built-in presets (brain_client/expressive/presets.py) mirrored for
// the browser until a shared presets.json exists; tests/expression.test.js fails
// when the two drift. Each is a recipe, so it previews through the same
// pipeline as anything typed.

/** @typedef {{ name: string, prompt: string, idea: string, recipe: string, keywords: string[] }} Preset */

/** @type {Preset[]} */
export const PRESETS = [
  {
    name: "happy",
    prompt: "happy. Something lovely just happened.",
    idea: "Rise tall and open, bounce on the beat, sway the cocked gripper, settle bright.",
    recipe:
      "go .4 z=.5 x=.4 p=.6 g=.6 a=.2 E=3 | osc 2 z .2 .5 E=4 | osc 1.5 k .3 .75 E=3 | go .6 z=.3 x=.2 p=.4 g=.4 k=0 E=1.5 | hold .6 E=1",
    keywords: ["happy", "joy", "glad", "cheerful", "delighted", "yay", "smile", "great"],
  },
  {
    name: "sad",
    prompt: "sad. You just heard disappointing news.",
    idea: "Sink slowly: the arm droops and folds, gaze to the floor, drifting back and away.",
    recipe: "go 1.8 z=-.7 x=-.5 p=-.8 a=-.2 g=.05 d=-.05 b=-8 E=.4 | hold 2 E=.2 | osc 2.5 k .12 2.5 E=.3 | hold 1",
    keywords: [
      "sad",
      "unhappy",
      "down",
      "disappointed",
      "deflated",
      "sorry",
      "gloomy",
      "lonely",
      "heartbroken",
      "miss",
    ],
  },
  {
    name: "curious",
    prompt: "curious. Something new caught your eye.",
    idea: "Lean in and look up, cock the gripper one way then the other, edge closer.",
    recipe:
      "go .5 p=.6 a=.5 k=.6 z=.2 g=.3 d=.06 E=1.5 | hold .8 E=.6 | go .4 k=-.6 E=1.8 | hold .8 E=.6 | go .4 k=.3 a=.7 d=.1 E=1.2 | hold .7 | go .8 a=.2 k=0 d=0 p=.3 E=.8",
    keywords: ["curious", "interested", "what", "wonder", "intrigued", "hmm", "new", "look"],
  },
  {
    name: "excited",
    prompt: "excited. You can hardly wait.",
    idea: "Snap tall and wide with the mouth open, bounce, wiggle the base, chatter, then simmer.",
    recipe:
      "go .25 z=.8 x=.7 p=.7 g=.8 E=6 | osc 1.6 z .25 .45 E=7 | osc 1.2 b 12 .6 E=7 | go .3 a=.5 d=.08 E=5 | osc 1 g .3 .4 | go .6 z=.4 x=.3 a=0 d=0 g=.4 E=2",
    keywords: ["excited", "thrilled", "can't wait", "wow", "awesome", "party", "hooray"],
  },
  {
    name: "proud",
    prompt: "proud. You finally solved it.",
    idea: "Rise to a mast and open up, chin up, a slow satisfied turn, then hold the pose.",
    recipe:
      "go 1 z=.9 x=.5 p=.6 a=-.1 g=.3 E=1 | hold 1.5 E=.5 | osc 1.6 b 8 1.6 E=.7 | go .8 z=.7 p=.4 E=.6 | hold .8",
    keywords: ["proud", "solved", "did it", "confident", "accomplished", "nailed", "success"],
  },
  {
    name: "confused",
    prompt: "confused. That makes no sense.",
    idea: "Hold the gripper up and tilt it one way, then the other with a small turn, half-fold, give up and reset.",
    recipe:
      "go .6 k=.7 p=.3 a=.35 z=.3 g=.2 E=1 | hold .9 E=.5 | go .5 k=-.6 p=.1 b=-10 E=1.2 | hold .9 | go .5 k=.4 b=8 z=.15 x=-.2 E=1 | hold .6 | go .7 k=0 b=0 a=0 z=0 x=0 p=0 E=.8",
    keywords: ["confused", "puzzled", "huh", "don't understand", "lost", "strange", "weird"],
  },
  {
    name: "surprised",
    prompt: "surprised. That came out of nowhere.",
    idea: "A beat of stillness, then a snap up and back with the mouth wide open, freeze, recover.",
    recipe:
      "go .5 E=.5 | hold .3 | go .15 z=.8 a=-.6 x=.5 p=.8 g=.9 d=-.06 E=9 | hold 1.2 E=1 | go .8 z=.3 a=0 x=.1 p=.3 g=.4 d=-.04 E=1.5 | hold .6",
    keywords: ["surprised", "surprise", "whoa", "oh", "shocked", "startled", "unexpected", "gasp"],
  },
  {
    name: "scared",
    prompt: "scared. Something big is coming at you.",
    idea: "Recoil and shrink, back away and turn aside, trembling, eyes still on the threat.",
    recipe:
      "go .2 a=-.8 z=-.3 x=-.6 p=.4 g=.05 d=-.12 E=8 | hold .6 E=6 | go .8 z=-.6 x=-.8 d=-.18 b=-15 E=5 | hold 1.5 E=6 | go 1 a=-.4 z=-.4 x=-.5 E=3",
    keywords: ["scared", "scary", "afraid", "fear", "frightened", "terrified", "nervous", "anxious", "danger"],
  },
  {
    name: "angry",
    prompt: "angry. You have had enough.",
    idea: "Square up head down, then two biting lunges forward with the gripper, glare, ease off.",
    recipe:
      "go .4 z=.4 a=.6 x=.3 p=-.2 g=0 E=3 | go .15 a=.9 d=.08 g=.9 E=9 | go .3 a=.6 g=.1 E=6 | go .15 a=.9 d=.12 g=.9 E=9 | go .4 a=.5 d=.05 g=.05 E=4 | hold .8 E=3 | go .8 a=.2 z=.2 x=0 d=0 E=1.5",
    keywords: ["angry", "mad", "furious", "annoyed", "enough", "frustrated", "rage", "grr"],
  },
  {
    name: "sleepy",
    prompt: "sleepy. You keep nodding off.",
    idea: "Droop heavier and heavier, jolt awake, then sink again and stay down.",
    recipe:
      "go 1.5 z=-.4 p=-.5 x=-.3 g=.1 E=.4 | go 1.2 z=-.8 p=-.9 E=.2 | go .3 z=-.2 p=-.1 E=2 | hold .5 E=.6 | go 1.8 z=-.85 p=-1 a=-.1 g=.05 E=.2 | hold 1.5 E=.1",
    keywords: ["sleepy", "tired", "sleep", "yawn", "exhausted", "drowsy", "bed", "night"],
  },
  {
    name: "agreeing",
    prompt: "nodding yes. You agree.",
    idea: "Lean in a little and nod in clear beats, then settle attentive.",
    recipe: "go .3 p=.4 a=.3 g=.25 E=1.5 | osc 2.4 p .35 .6 E=1.5 | go .5 p=.2 a=.1 E=.8 | hold .5",
    keywords: ["yes", "agree", "nod", "pleased", "okay", "ok", "sure", "right", "exactly", "understood", "got it"],
  },
  {
    name: "disagreeing",
    prompt: "shaking your head no. You refuse.",
    idea: "Pull back slightly and shake the whole body side to side, then stop square.",
    recipe: "go .3 p=.2 a=-.2 x=-.2 E=1.5 | osc 2.4 b 14 .6 E=2 | go .5 b=0 a=0 x=0 E=.8 | hold .4",
    keywords: ["no", "disagree", "refuse", "nope", "shake", "never", "wrong", "don't"],
  },
  {
    name: "thinking",
    prompt: "thinking. Let me figure this out.",
    idea: "Raise the gripper toward the head and look up, a slow tilt, a small turn away while pondering, back.",
    recipe:
      "go .8 p=.5 k=.4 z=.45 a=.1 g=.1 E=.6 | hold 1.2 E=.4 | osc 2 k .15 1.4 E=.5 | go .6 p=.3 k=.6 b=-10 | hold 1 E=.3 | go .8 p=0 k=0 b=0 a=0 z=0 E=.6",
    keywords: ["thinking", "think", "consider", "ponder", "figure", "let me see", "wondering", "plan"],
  },
  {
    name: "affectionate",
    prompt: "affectionate. You are happy to see a friend.",
    idea: "Lean in close and look up softly, sway the tilted gripper gently, stay near.",
    recipe:
      "go 1 a=.7 p=.5 k=.4 x=.2 g=.35 d=.08 E=.8 | osc 2.4 k .25 1.2 E=.8 | hold .8 E=.5 | go 1 a=.4 k=.1 d=.04 E=.6",
    keywords: ["affectionate", "love", "friend", "hug", "cute", "sweet", "miss you", "welcome", "hello", "hi"],
  },
  {
    name: "bored",
    prompt: "bored. Nothing is happening.",
    idea: "Sag, look away one way and the other, chew on nothing, sag again.",
    recipe:
      "go 1.2 z=-.3 p=-.3 x=-.2 E=.4 | hold 1 E=.2 | go .8 b=-20 p=.1 | hold .8 | go .8 b=12 | osc 2 g .15 1 E=.3 | go 1 b=0 p=-.3 E=.2 | hold .8",
    keywords: ["bored", "boring", "meh", "whatever", "waiting", "dull", "nothing"],
  },
  {
    name: "relieved",
    prompt: "relieved. Phew, it worked out.",
    idea: "Draw up as if inhaling, then let it all out in a long slump, and come back to easy.",
    recipe:
      "go .8 z=.4 p=.5 x=.3 g=.5 E=1.5 | go 1.2 z=-.3 p=-.3 x=-.2 g=.2 a=-.1 E=.6 | hold .8 E=.4 | go 1 z=0 p=.1 x=0 a=0 g=.15 E=.6",
    keywords: ["relieved", "relief", "phew", "finally", "safe", "calm", "worked"],
  },
];

export const DEFAULT_PRESET = "curious";

/**
 * The preset whose keywords best match `prompt` — what plays at once while a
 * planner thinks (the core's presets.match: whole-word keyword hits, first
 * best wins, DEFAULT_PRESET when nothing matches).
 * @param {string} prompt @returns {Preset}
 */
export function nearestPreset(prompt) {
  const text = ` ${prompt.toLowerCase().replace(/[^a-z']+/g, " ")} `;
  let best = /** @type {Preset | null} */ (null);
  let bestScore = 0;
  for (const preset of PRESETS) {
    const score = preset.keywords.filter((k) => text.includes(` ${k} `)).length;
    if (score > bestScore) {
      best = preset;
      bestScore = score;
    }
  }
  return best ?? /** @type {Preset} */ (PRESETS.find((p) => p.name === DEFAULT_PRESET));
}
