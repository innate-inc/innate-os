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
    idea: "Throw the arm up and out with the claw open, bounce and sway side to side like a wave, settle bright.",
    recipe:
      "go .35 x=.9 z=.5 p=.6 g=.8 E=2 | osc 1.8 b 14 .6 E=3 | osc 1.5 z .25 .5 E=3 | go .5 x=.6 z=.3 p=.4 g=.5 E=1.5 | hold .5 E=1",
    keywords: ["happ", "joy", "joyf", "glad", "cheer", "delight", "yay", "smil", "great", "wonderful", "fun", "laugh", "celebrat", "song", "music", "danc", "play"],
  },
  {
    name: "sad",
    prompt: "sad. You just heard disappointing news.",
    idea: "Sink at once: the arm sags and the claw hangs, the head drops to the floor, then turn a little away and stay there.",
    recipe:
      "go 1 z=-1 p=-1 g=0 E=.2 | hold 1 E=.1 | go 1.5 b=-20 d=-.06 E=.1 | hold 3 E=.05",
    keywords: ["sad", "unhapp", "down", "disappoint", "deflat", "sorry", "gloom", "lonel", "heartbr", "miss", "cry", "tear", "grie", "depress", "upset", "blue", "mourn", "embarrass", "ashamed", "guilt"],
  },
  {
    name: "curious",
    prompt: "curious. Something new caught your eye.",
    idea: "Reach toward it with the head up, cock the claw one way then the other, inch closer.",
    recipe:
      "go .6 a=.7 p=.7 k=.7 g=.35 E=1 | hold .7 E=.5 | go .4 k=-.7 E=1.2 | hold .7 E=.5 | go .5 a=.9 k=.3 d=.08 | hold .6 | go .8 a=.3 k=0 d=0 p=.4 E=.6",
    keywords: ["curio", "interest", "wonder", "intrigu", "hmm", "new", "look", "investigat", "sniff", "explor", "notic", "inspect", "peek", "smell", "hunt", "stalk", "what's that"],
  },
  {
    name: "excited",
    prompt: "excited. You can hardly wait.",
    idea: "Snap the arm up with the claw wide open, bounce fast, wiggle the whole body, chatter, then simmer.",
    recipe:
      "go .25 z=.8 x=.5 p=.8 g=.9 E=5 | osc 1.6 z .25 .4 E=6 | osc 1.2 b 15 .5 E=6 | osc 1 g .35 .35 | go .5 z=.4 x=.3 g=.5 E=2",
    keywords: ["excit", "thrill", "can't wait", "wow", "awesome", "party", "hooray", "eager", "pump", "ecstat", "bounc", "jump", "woohoo", "lottery", "dog"],
  },
  {
    name: "proud",
    prompt: "proud. You finally solved it.",
    idea: "Rise slowly to a tall open stance with the head held high and the chest pushed toward the person, then hold still.",
    recipe:
      "go 1.5 z=.7 x=.5 p=1 d=.06 g=.2 E=.3 | hold 2 E=.15 | go 1 b=12 E=.2 | hold 1 | go 1 b=0 E=.2 | hold .6",
    keywords: ["proud", "pride", "solved", "did it", "confiden", "accomplish", "nailed", "success", "triumph", "victor", "win", "champion", "brag", "smug", "applau", "soldier"],
  },
  {
    name: "confused",
    prompt: "confused. That makes no sense.",
    idea: "Head up, look one way and then the other with the claw tipping each way, a puzzled pause, look again.",
    recipe:
      "go .5 p=.5 a=.3 b=-20 k=.7 g=.3 E=.8 | hold .7 E=.4 | go .5 b=20 k=-.7 | hold .7 | go .4 b=-10 k=.5 g=.5 | hold .5 | go .6 b=0 k=0 p=.3 a=.1 g=.2 E=.5 | hold .6",
    keywords: ["confus", "puzzl", "huh", "don't understand", "lost", "strange", "weird", "baffl", "perplex", "unsure", "doubt", "bewilder", "dizz", "drunk", "tipsy"],
  },
  {
    name: "surprised",
    prompt: "surprised. That came out of nowhere.",
    idea: "A beat of stillness, then the body jerks back, the arm snaps in, head flies up and the claw gapes; freeze, slowly recover.",
    recipe:
      "hold .4 | go .15 a=-1 p=1 g=1 d=-.1 E=8 | hold 1.2 E=1 | go 1 a=-.3 p=.4 g=.4 d=-.08 E=1 | hold .6",
    keywords: ["surpris", "whoa", "shock", "startl", "unexpected", "gasp", "astonish", "amaz", "wait what", "caught", "sneez"],
  },
  {
    name: "scared",
    prompt: "scared. Something big is coming at you.",
    idea: "Shrink and get away: the arm pulls in, the head drops, the body backs off and turns aside, trembling.",
    recipe:
      "go .3 a=-1 x=-1 z=-.8 p=-1 g=0 d=-.15 b=-30 E=7 | go .8 d=-.25 b=-45 E=8 | hold 2.5 E=8 | go 1 p=-.7 E=4",
    keywords: ["scar", "afraid", "fear", "frighten", "terrif", "nervous", "anxi", "danger", "panic", "flinch", "cower", "spider", "monster", "hid", "shy", "stage"],
  },
  {
    name: "angry",
    prompt: "angry. You have had enough.",
    idea: "Square up with the head lowered in a glare, then lunge at the person with the claw snapping, twice, and hold the glare.",
    recipe:
      "go .4 a=.6 p=-.8 x=-.2 g=.9 E=3 | go .2 d=.12 a=.9 g=0 E=9 | go .3 d=.02 a=.6 g=.9 E=4 | go .2 d=.15 a=.9 g=0 E=9 | go .4 d=.06 a=.6 g=.6 E=4 | hold 1 E=3 | go .8 a=.3 p=-.4 g=.2 E=2",
    keywords: ["angr", "mad", "furious", "annoy", "enough", "frustrat", "rage", "grr", "irritat", "hate", "grump", "fury", "livid", "disgust", "gross", "yuck", "jealous", "snake", "shoo"],
  },
  {
    name: "sleepy",
    prompt: "sleepy. You keep nodding off.",
    idea: "Sink heavier and heavier until the head hangs, jerk half awake, then sink for good, turned a little away.",
    recipe:
      "go 2 z=-.8 p=-1 g=0 b=-15 E=.1 | go .4 z=-.3 p=-.3 E=.8 | go 2 z=-1 p=-1 b=-25 E=0 | hold 2.5 E=0",
    keywords: ["sleep", "tired", "yawn", "exhaust", "drows", "bed", "night", "nap", "doz", "snooz", "weary"],
  },
  {
    name: "agreeing",
    prompt: "nodding yes. You agree.",
    idea: "Lean in and nod with head and arm together, in clear beats, then settle attentive.",
    recipe:
      "go .3 p=.5 a=.4 g=.25 E=1 | go .25 p=-.2 a=.55 z=-.15 | go .25 p=.5 a=.4 z=0 | go .25 p=-.2 a=.55 z=-.15 | go .25 p=.5 a=.4 z=0 | go .25 p=-.2 a=.55 z=-.15 | go .4 p=.3 a=.3 z=0 E=.6 | hold .4",
    keywords: ["yes", "agree", "nod", "pleased", "okay", "ok", "sure", "right", "exactly", "understood", "got it", "correct", "indeed", "absolutely"],
  },
  {
    name: "disagreeing",
    prompt: "shaking your head no. You refuse.",
    idea: "Pull the arm in, then turn the whole body firmly side to side, three times, and stop square.",
    recipe:
      "go .3 p=0 x=-.8 a=-.3 E=1 | go .3 b=25 p=-.2 | go .4 b=-25 | go .4 b=25 | go .4 b=-25 | go .4 b=25 | go .4 b=-25 | go .5 b=0 x=0 a=0 E=.5 | hold .4",
    keywords: ["no", "disagree", "refus", "nope", "shake", "never", "wrong", "don't", "deny", "reject", "nah", "not"],
  },
  {
    name: "thinking",
    prompt: "thinking. Let me figure this out.",
    idea: "Bring the claw up beside the head and look up and away, roll it slowly while pondering, then come back.",
    recipe:
      "go .9 a=-.7 p=.5 k=.4 g=.1 E=.5 | hold 1.2 E=.3 | osc 2.4 k .2 1.2 E=.4 | go .6 b=-10 p=.6 | hold 1 E=.3 | go .9 a=0 k=0 b=0 p=0 E=.5",
    keywords: ["think", "consider", "ponder", "figure", "let me see", "plan", "calculat", "reflect", "decid", "idea", "rememb", "wondering"],
  },
  {
    name: "affectionate",
    prompt: "affectionate. You are happy to see a friend.",
    idea: "Rise softly toward the friend with the head up and the claw half open, sway the tilted claw, lean closer.",
    recipe:
      "go 1.2 a=.5 z=.5 p=1 g=.4 d=.15 E=.3 | osc 3 k .35 1.5 E=.3 | go 1 a=.7 z=.2 d=.2 | hold 1.2 E=.2",
    keywords: ["affection", "love", "friend", "hug", "cute", "sweet", "miss you", "welcome", "hello", "hi", "adore", "cuddl", "fond", "thank", "grateful", "greet"],
  },
  {
    name: "bored",
    prompt: "bored. Nothing is happening.",
    idea: "Sag with the head down, chew on nothing for a while, then turn away and stay slumped.",
    recipe:
      "go 2 z=-.5 a=-.3 p=-.5 E=.1 | osc 4 g .2 2 E=.1 | hold 2 E=0 | go 1.5 b=-25 | hold 2 E=0",
    keywords: ["bore", "meh", "whatever", "wait", "dull", "nothing", "tedious", "sigh", "impatien", "idle"],
  },
  {
    name: "relieved",
    prompt: "relieved. Phew, it worked out.",
    idea: "Tense and tucked for a moment, then a long exhale: everything loosens, sinks a little and settles calm.",
    recipe:
      "go .6 a=-.4 p=.5 g=.1 E=1.5 | hold .5 E=1 | go 2 a=0 p=0 z=-.3 g=.2 E=.1 | hold 1.5 E=0 | go 1 z=0 p=.2 E=.1 | hold 1 E=0",
    keywords: ["relie", "phew", "finally", "safe", "calm", "worked", "relax", "ease", "breath"],
  },
  {
    name: "listening",
    prompt: "listening. Someone is talking to you.",
    idea: "Turn the attention up to the speaker: a slight lean in, head raised, a small acknowledging nod.",
    recipe:
      "go .8 a=.25 p=.6 k=.15 E=.6 | hold 1.5 E=.4 | go .4 p=.4 | go .4 p=.65 | hold 1 E=.4 | go .8 a=.1 p=.4 k=0 E=.5",
    keywords: ["listen", "attent", "hear", "tell me", "go on", "i see", "pay attention", "focus"],
  },
];

export const DEFAULT_PRESET = "listening";

/**
 * Whether `keyword` hits the prompt (the core's presets._hits): a phrase as a
 * phrase, a keyword of 4+ letters as the start of any word, a shorter one as a
 * whole word.
 * @param {string} keyword @param {string[]} words @param {string} text
 */
function hits(keyword, words, text) {
  if (keyword.includes(" ")) return text.includes(` ${keyword} `);
  if (keyword.length >= 4) return words.some((w) => w.startsWith(keyword));
  return words.includes(keyword);
}

/**
 * The preset whose keywords best match `prompt` — what plays at once while a
 * planner thinks (the core's presets.match: first best wins, DEFAULT_PRESET
 * when nothing matches).
 * @param {string} prompt @returns {Preset}
 */
export function nearestPreset(prompt) {
  const text = ` ${prompt.toLowerCase().replace(/[^a-z']+/g, " ")} `;
  const words = text.split(" ").filter(Boolean);
  let best = /** @type {Preset | null} */ (null);
  let bestScore = 0;
  for (const preset of PRESETS) {
    const score = preset.keywords.filter((k) => hits(k, words, text)).length;
    if (score > bestScore) {
      best = preset;
      bestScore = score;
    }
  }
  return best ?? /** @type {Preset} */ (PRESETS.find((p) => p.name === DEFAULT_PRESET));
}
