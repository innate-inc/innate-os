// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Agent Studio (v0): what the selected agent can do, one switch per skill.
// During the Nowhere story the world decides which skill may be granted next;
// the grant itself is offered as a chip in the chat and mirrored here, and the
// panel ends as the reveal of the agent the visitor built.

import { renderAgent } from "./agentFile.js";
import { highlightPython } from "./pythonHighlight.js";

const STORY_AGENT = "void_agent";
const GRADUATION_AGENT = "demo_agent";
const SKIP_KEY = "innate.nowhere.skip.v1";
const ARMED_KEY = "innate.nowhere.armed";
const INTERNAL_SKILLS = new Set(["innate-os/suggest_user_prompts"]);
// Never mentioned by the story; stays off until someone asks for it.
const UNMENTIONED_SKILLS = new Set(["innate-os/open_gripper"]);
const WAVE = "innate-os/wave";
const MEMORY = "innate-os/search_memory";
const GRADUATION_MAX_WAIT_MS = 25_000;
// Acts the world opens with a line, so MARS speaks before the visitor is asked to move;
// the give-up path has no "Got it" to hand the brain its turn.
const ACT_OPENERS = /** @type {Record<string, string>} */ ({
  "Pick up the bar": "Something just landed on the floor in front of you.",
  "Who am I": "That thing is beside the point. Who are you, anyway?",
  "Go through the door": "A door. Standing on its own, right there.",
});
// A grant is a turn for the brain, not only a toolset change: the chip says it out loud.
const GRANT_LINES = /** @type {Record<string, string>} */ ({
  "innate-os/head_emotion": "Here. You have a face now.",
  "innate-os/turn_in_place": "Fine, you can turn now.",
  "innate-os/move_straight": "You can drive now. Go on.",
  "innate-os/pick_any_object": "You have hands. Pick it up.",
  "innate-os/navigate_to_position": "Navigation is yours. Go to the door.",
});

/** @param {string} id */
const leaf = (id) => id.split("/").at(-1) ?? id;
/** @param {string} id */
const skillLabel = (id) => leaf(id).replace(/_/g, " ");
/** @param {string} id */
const className = (id) => leaf(id).split(/[-_]+/).filter(Boolean).map((p) => p[0].toUpperCase() + p.slice(1)).join("");

/** @param {Storage} store @param {string} key */
function read(store, key) {
  try {
    return store.getItem(key);
  } catch {
    return null;
  }
}
/** @param {Storage} store @param {string} key @param {string} value */
function write(store, key, value) {
  try {
    store.setItem(key, value);
  } catch {
    /* a locked-down browser just replays the intro */
  }
}

/**
 * @param {HTMLElement} root
 * @param {ReturnType<typeof import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {any} session sim session exposing onChallenge/onEnvironment/startChallenge, or null on hardware
 * @param {ReturnType<typeof import("./agentPanel.js").createAgentPanel>} panel
 * @param {{ transcript: () => string[], cancelSkill: () => Promise<unknown> }} opts robot lines of this page, oldest first; a way to stop the running skill
 * @returns {{ destroy: () => void }}
 */
export function createAgentStudio(root, agentState, session, panel, opts) {
  const el = document.createElement("section");
  el.className = "agent-studio";
  el.setAttribute("aria-label", "Agent Studio");
  const head = document.createElement("div");
  head.className = "agent-studio-head";
  const eyebrow = document.createElement("span");
  eyebrow.className = "microlabel";
  eyebrow.textContent = "Agent Studio";
  const title = document.createElement("strong");
  title.className = "agent-studio-title";
  const persona = document.createElement("span");
  persona.className = "agent-studio-persona";
  head.append(eyebrow, title, persona);
  const note = document.createElement("p");
  note.className = "agent-studio-note";
  const nameRow = document.createElement("form");
  nameRow.className = "agent-studio-name";
  nameRow.hidden = true;
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.maxLength = 40;
  nameInput.placeholder = "Give it a name";
  nameInput.setAttribute("aria-label", "Robot name");
  const nameButton = document.createElement("button");
  nameButton.type = "submit";
  nameButton.textContent = "Name it";
  nameRow.append(nameInput, nameButton);
  const list = document.createElement("ul");
  list.className = "agent-studio-skills";
  const actions = document.createElement("div");
  actions.className = "agent-studio-actions";
  const foot = document.createElement("p");
  foot.className = "agent-studio-foot";
  el.append(head, note, nameRow, list, actions, foot);
  root.append(el);

  // The agent file, as a sheet over the stage: the ownership payoff.
  const sheet = document.createElement("section");
  sheet.className = "agent-code-sheet";
  sheet.hidden = true;
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-label", "The agent you built");
  const sheetHead = document.createElement("div");
  sheetHead.className = "agent-code-head";
  const sheetTitle = document.createElement("strong");
  const sheetPath = document.createElement("span");
  const sheetClose = document.createElement("button");
  sheetClose.type = "button";
  sheetClose.textContent = "Close";
  sheetClose.addEventListener("click", () => {
    showCode = false;
    render(true);
  });
  sheetHead.append(sheetTitle, sheetPath, sheetClose);
  const sheetBody = document.createElement("pre");
  sheetBody.className = "agent-code-body mono";
  const sheetFoot = document.createElement("div");
  sheetFoot.className = "agent-code-foot";
  sheetFoot.textContent = "This file runs unchanged on a real MARS. Drop it in workspace/custom_agents and it is in the agent list.";
  sheet.append(sheetHead, sheetBody, sheetFoot);
  root.append(sheet);

  // The door: the room goes white while the next world compiles behind it.
  const whiteout = document.createElement("div");
  whiteout.className = "agent-whiteout";
  whiteout.setAttribute("aria-hidden", "true");
  root.append(whiteout);
  let whiteUntil = 0;

  /** @type {any} */
  let challenge = null;
  /** @type {any} */
  let environment = null;
  let armedAttempt = read(sessionStorage, ARMED_KEY) ?? "";
  let graduatedAttempt = "";
  let graduationReady = false;
  /** @type {ReturnType<typeof setInterval> | null} */
  let graduationPoll = null;
  let giftedWaveAttempt = "";
  let giftedMemoryAttempt = "";
  let lastChoice = { text: "", at: 0 };
  let autoStarted = false;
  let showCode = false;
  let seenAct = -1;
  let cameraSide = 0;
  let doorAttempt = "";
  let canWaitFrom = -1;
  // Which transcript lines make the story card: set as the acts pass.
  const marks = { opener: 0, can: -1, persona: -1 };

  const active = () => challenge?.active ?? null;
  const story = () => (active()?.runtime?.story ? active() : null);
  const runtime = () => story()?.runtime ?? null;
  /** Persona and name: from the running story, else what the world carried over. */
  const profile = () => {
    const r = runtime();
    const p = challenge?.profile ?? {};
    return { persona: r?.persona || p.persona || "", name: r?.name || p.name || "" };
  };
  const currentAgent = () => {
    const s = agentState.get();
    return s.agents.find((a) => a.id === s.currentDirective) ?? null;
  };
  const storyAgent = () => agentState.get().agents.find((a) => a.id === STORY_AGENT) ?? null;
  const grantedSkills = () => [...agentState.get().activeSkills].filter((id) => !INTERNAL_SKILLS.has(id));
  const skipped = () => read(localStorage, SKIP_KEY) === "1";
  const switching = () => environment?.switch?.state === "loading";
  const hasSpoken = () => opts.transcript().length > marks.opener;

  // A visit that lands in Nowhere with nothing running is a first run: begin it.
  function autoStart() {
    if (autoStarted || !session || skipped() || environment?.environment?.id !== "void" || !challenge || challenge.active) return;
    if (!challenge.list?.some((/** @type {any} */ c) => c.id === "nowhere")) return;
    autoStarted = true;
    session.startChallenge("nowhere");
  }

  // The story's agent arms itself; nobody should have to find a switch. A
  // just-started attempt arms fresh (which resets chat, memory and skills); an
  // attempt already under way is resumed as is, so a reload changes nothing.
  function autoArm() {
    const s = story();
    if (!s || armedAttempt === s.attempt_id || !storyAgent()) return;
    armedAttempt = s.attempt_id;
    write(sessionStorage, ARMED_KEY, armedAttempt);
    const elapsedMs = Math.max(0, Number(s.elapsed_s) || 0) * 1000;
    const fresh = elapsedMs < 8000 || agentState.get().currentDirective !== STORY_AGENT;
    if (fresh) void agentState.setDirective(STORY_AGENT);
    panel.beginOnboarding(fresh, Date.now() - elapsedMs);
    marks.opener = opts.transcript().length;
    marks.can = -1;
    marks.persona = -1;
    seenAct = -1;
    canWaitFrom = -1;
    setCamera(0);
  }

  /** @param {number} side */
  function setCamera(side) {
    cameraSide = side;
    document.dispatchEvent(new CustomEvent("innate:camera-mode", { detail: { mode: "chase", side } }));
  }

  /** @param {string} skill @param {boolean} [spoken] */
  function grant(skill, spoken = true) {
    const next = new Set(agentState.get().activeSkills);
    next.add(skill);
    agentState.setActiveSkills([...next]);
    panel.clearSuggestedPrompts();
    if (spoken) void panel.submitText(GRANT_LINES[skill] ?? `Here: ${skillLabel(skill)}.`);
  }

  // Wave arrives as a gift the moment the persona lands, so the robot can greet in character.
  function giftWave() {
    const s = story();
    if (!s || !profile().persona || giftedWaveAttempt === s.attempt_id) return;
    giftedWaveAttempt = s.attempt_id;
    if (!agentState.get().activeSkills.has(WAVE)) grant(WAVE, false);
  }

  // Memory arrives in the Backrooms: the place is familiar, and a question gives the brain its turn.
  function giftMemory() {
    const out = active()?.id === "way_out" ? active() : null;
    if (!out || switching() || environment?.environment?.id !== "backrooms" || giftedMemoryAttempt === out.attempt_id) return;
    if (agentState.get().currentDirective !== STORY_AGENT) return;
    giftedMemoryAttempt = out.attempt_id;
    const next = new Set(agentState.get().activeSkills);
    next.add(MEMORY);
    agentState.setActiveSkills([...next]);
    panel.clearSuggestedPrompts();
    setTimeout(() => void panel.narrate("Somewhere else. Yellow, this time."), 2500);
  }

  /** @param {{persona?: string, name?: string}} choice */
  function choose(choice) {
    const text = choice.persona ?? choice.name ?? "";
    if (text === lastChoice.text && Date.now() - lastChoice.at < 5000) return;
    lastChoice = { text, at: Date.now() };
    session?.sendChallengeEvent?.({ type: "persona", ...choice });
    if (choice.persona) void panel.submitText(`From now on, you are ${choice.persona}.`);
    else if (choice.name) void panel.submitText(`Your name is ${choice.name}.`);
  }
  nameRow.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = nameInput.value.trim();
    if (!name) return;
    choose({ name });
    nameInput.value = "";
  });

  let skipGreetingPending = false;
  function skipIntro() {
    write(localStorage, SKIP_KEY, "1");
    session?.abortChallenge?.();
    // The visitor chose not to have that story: none of it stays on screen.
    panel.beginOnboarding(true, Date.now());
    panel.setOffers([]);
    void agentState.setDirective(GRADUATION_AGENT);
    session?.switchEnvironment?.("apartment");
    skipGreetingPending = true;
  }
  // Once the apartment is in, one line gives the demo agent something better to open with.
  function greetAfterSkip() {
    if (!skipGreetingPending || switching() || environment?.environment?.id !== "apartment") return;
    if (agentState.get().currentDirective !== GRADUATION_AGENT || !agentState.get().brainActive) return;
    skipGreetingPending = false;
    setTimeout(() => void panel.submitText("I skipped the tutorial. Who are you, and where is this?", { quiet: true }), 1500);
  }

  // Back to the white room from anywhere: forget the skip, drop the current attempt,
  // and let auto-start begin a fresh one once the world is Nowhere again.
  function restartIntro() {
    try {
      localStorage.removeItem(SKIP_KEY);
      sessionStorage.removeItem(ARMED_KEY);
    } catch {
      /* fine */
    }
    armedAttempt = "";
    autoStarted = false;
    graduationReady = false;
    showCode = false;
    panel.setOffers([]);
    session?.abortChallenge?.();
    if (environment?.environment?.id !== "void") session?.switchEnvironment?.("void");
    render(true);
  }

  /** Everything the story handed out; nothing it never mentioned. */
  function earnedSkills() {
    return (storyAgent()?.skills ?? []).filter((id) => !UNMENTIONED_SKILLS.has(id));
  }

  function graduate() {
    agentState.setActiveSkills(earnedSkills());
    session?.switchEnvironment?.("apartment");
  }

  function storyCard() {
    const { persona: who, name } = profile();
    const lines = opts.transcript();
    const pick = (/** @type {number} */ i) => (i >= 0 && i < lines.length ? lines[i] : null);
    const candidates = [pick(marks.can), pick(marks.persona), lines.at(-1) ?? null, pick(marks.opener)]
      .filter((t, i, all) => typeof t === "string" && t.length < 200 && all.indexOf(t) === i)
      .slice(0, 3);
    const chosen = [pick(marks.opener), ...candidates].filter((t, i, all) => t && candidates.includes(t) && all.indexOf(t) === i);
    return [
      `I gave a robot its body one skill at a time. Meet ${name || "MARS"}${who ? `, ${who}` : ""}.`,
      `Skills it earned: ${grantedSkills().map(skillLabel).join(", ") || "none yet"}.`,
      ...chosen.map((t) => `"${t}"`),
      "Build your own: sim-demo.innate.bot",
    ].join("\n");
  }

  async function copyStory() {
    try {
      await navigator.clipboard.writeText(storyCard());
      foot.textContent = "Copied. Paste it anywhere.";
    } catch {
      foot.textContent = storyCard();
    }
    render(true);
  }

  function agentSource() {
    const { persona: who, name: given } = profile();
    const name = given || "MARS";
    const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "mars";
    const prompt = who
      ? `You are ${name}, a small robot with an arm, and you are ${who}. Stay in character, keep replies short, and use a head emotion whenever you speak.`
      : `You are ${name}, a small robot with an arm. Dry, quick, helpful. Keep replies short and use a head emotion whenever you speak.`;
    const skillIds = grantedSkills();
    const imports = new Map(skillIds.map((sid) => [sid, { module: `innate_skills.${leaf(sid)}`, className: className(sid) }]));
    return { id, source: renderAgent({ id, displayName: name, prompt, skillIds, listen: true, gaze: true }, imports, `${name}, built in the first five minutes.`) };
  }

  function offers() {
    const r = runtime();
    const out = active()?.id === "way_out" ? active() : null;
    if (switching() || out?.state === "passed") return { chips: [], exclusive: true }; // nothing lands while the world changes
    if (!r || agentState.get().currentDirective !== STORY_AGENT || !hasSpoken()) return { chips: [], exclusive: false };
    const granted = agentState.get().activeSkills;
    /** @type {Array<{text: string, kind: string, onSelect: (text: string) => void}>} */
    const chips = [];
    if (ACT_OPENERS[r.label] && opts.transcript().length <= canWaitFrom) return { chips: [], exclusive: false }; // MARS speaks first
    const personas = r.personas ?? [];
    if (personas.length) {
      // The persona choice owns the row: nothing else competes with it.
      for (const p of personas) chips.push({ text: p, kind: "persona", onSelect: () => choose({ persona: p }) });
      chips.push({ text: "Surprise me", kind: "persona", onSelect: () => choose({ persona: personas[Math.floor(Math.random() * personas.length)] }) });
      return { chips, exclusive: true };
    }
    for (const skill of r.wants ?? []) {
      if (granted.has(skill) || INTERNAL_SKILLS.has(skill) || skill === WAVE) continue;
      chips.push({ text: `Give it ${skillLabel(skill)}`, kind: "grant", onSelect: () => grant(skill) });
    }
    return { chips, exclusive: false };
  }

  /** @param {string} text @param {() => void} onClick @param {string} [kind] */
  function button(text, onClick, kind = "") {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `agent-studio-action ${kind}`;
    b.textContent = text;
    b.addEventListener("click", onClick);
    return b;
  }

  // The diploma waits for the ending: no skill running and the robot's line delivered.
  /** @param {any} out */
  function armGraduation(out) {
    graduatedAttempt = out.attempt_id;
    graduationReady = false;
    // Out means out: whatever the robot was doing stops, and it hears it before it speaks again.
    panel.clearSuggestedPrompts();
    void opts.cancelSkill().catch(() => {});
    setTimeout(() => void panel.narrate("You're out. You made it."), 600);
    const spokenBefore = opts.transcript().length;
    const deadline = Date.now() + GRADUATION_MAX_WAIT_MS;
    if (graduationPoll) clearInterval(graduationPoll);
    graduationPoll = setInterval(() => {
      const spoke = opts.transcript().length > spokenBefore;
      if ((spoke && !panel.isBusy()) || Date.now() > deadline) {
        if (graduationPoll) clearInterval(graduationPoll);
        graduationPoll = null;
        graduationReady = true;
        agentState.setActiveSkills(earnedSkills());
        const { name } = profile();
        panel.addNotice(`You built this agent${name ? `: ${name}` : ""}. Its skills are all yours now.`);
        render(true);
      }
    }, 500);
  }

  let renderedKey = "";
  /** @param {boolean} [force] */
  function render(force = false) {
    const s = agentState.get();
    const agent = currentAgent();
    const r = runtime();
    const out = active()?.id === "way_out" ? active() : null;
    const { persona: who, name } = profile();
    const env = environment?.environment?.id;
    // World frames arrive at physics rate; touch the DOM only when something shown here changed.
    const key = JSON.stringify([
      s.currentDirective, [...s.activeSkills].sort(), agent?.skills, r && { ...r, brief: undefined },
      out && { id: out.id, state: out.state, attempt_id: out.attempt_id }, who, name, env, switching(),
      showCode, foot.textContent, graduationReady, hasSpoken(), Date.now() < whiteUntil, opts.transcript().length > canWaitFrom,
    ]);
    if (!force && key === renderedKey) return;
    renderedKey = key;

    // Act bookkeeping: transcript marks for the story card, stale chips, the grasp camera.
    if (r && r.act !== seenAct) {
      if (seenAct >= 0) panel.clearSuggestedPrompts();
      if (ACT_OPENERS[r.label]) {
        canWaitFrom = opts.transcript().length;
        if (r.label === "Pick up the bar") marks.can = canWaitFrom;
        const line = ACT_OPENERS[r.label];
        setTimeout(() => void panel.narrate(line), 300);
      }
      seenAct = r.act;
    }
    if (who && marks.persona < 0 && r) marks.persona = opts.transcript().length;
    const wantSide = r?.label === "Pick up the bar" ? 0.9 : 0;
    if (r && wantSide !== cameraSide) setCamera(wantSide);

    // The door: the running skill stops, chips freeze, the world speaks, and it goes white until the Backrooms are in.
    const doorPassed = (r?.finished || (!r && seenAct === 5) || (switching() && env === "void")) && armedAttempt && doorAttempt !== armedAttempt;
    if (doorPassed) {
      doorAttempt = armedAttempt;
      panel.clearSuggestedPrompts();
      void opts.cancelSkill().catch(() => {});
      setTimeout(() => void panel.narrate("Through the door."), 400);
    }
    if (r?.finished || (switching() && env === "void")) whiteUntil = Date.now() + 6000;
    if (out && env === "backrooms" && !switching()) whiteUntil = Math.min(whiteUntil, Date.now() + 800);
    const white = Date.now() < whiteUntil;
    whiteout.classList.toggle("on", white);
    if (white) setTimeout(() => render(), whiteUntil - Date.now() + 50);

    if (out?.state === "passed" && graduatedAttempt !== out.attempt_id) armGraduation(out);
    const graduated = !!out && out.state === "passed" && graduationReady;

    const inStory = !!r;
    const storyAgentOn = s.currentDirective === STORY_AGENT;
    const storyUi = inStory || (!!out && !graduated) || (switching() && env === "void") || (storyAgentOn && env !== "apartment" && !graduated);
    document.body.classList.toggle("story-agent", storyAgentOn);
    const inVoid = env === "void";
    const shownName = name || (storyUi ? "MARS" : (agent?.name ?? "No agent selected"));
    title.textContent = agent || storyUi ? shownName : "No agent selected";
    panel.setDisplayName(storyAgentOn ? name || "MARS" : null);
    persona.textContent = who;
    persona.hidden = !who;
    el.classList.toggle("story", storyUi);
    root.classList.toggle("story-active", storyUi);
    document.body.classList.toggle("story-active", storyUi);
    nameRow.hidden = !(inStory && r.label === "Who am I");
    note.textContent = inStory
      ? r.finished
        ? "Through the door."
        : `Act ${r.act + 1} of ${r.acts}: ${r.label}`
      : graduated
        ? "It found the way out. This is the agent you built."
        : out
          ? "Find the way out."
          : agent
            ? "Flip a skill to add or remove it from this agent's tools."
            : "Pick an agent in the chat panel to see its skills.";

    list.replaceChildren();
    const roster = agent?.skills ?? [];
    const unlocked = new Set(r?.unlocked ?? []);
    const wanted = new Set(r?.wants ?? []);
    for (const id of roster) {
      if (INTERNAL_SKILLS.has(id)) continue;
      if (inStory && !unlocked.has(id)) continue; // unasked rows would spoil the arc
      if (out && UNMENTIONED_SKILLS.has(id) && !s.activeSkills.has(id)) continue;
      const granted = s.activeSkills.has(id);
      const row = document.createElement("li");
      row.className = "agent-studio-skill";
      row.classList.toggle("wanted", inStory && wanted.has(id) && !granted);
      row.classList.toggle("granted", granted);
      const label = document.createElement("span");
      label.className = "agent-studio-skill-name";
      label.textContent = skillLabel(id);
      const state = document.createElement("span");
      state.className = "agent-studio-skill-state microlabel";
      state.textContent = granted ? "granted" : inStory ? "wanted" : "off";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "agent-studio-toggle";
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-checked", String(granted));
      toggle.setAttribute("aria-label", `${granted ? "Revoke" : "Grant"} ${skillLabel(id)}`);
      toggle.addEventListener("click", () => agentState.toggleSkill(id));
      row.append(label, state, toggle);
      list.append(row);
    }
    if (inStory && !list.childElementCount) {
      const empty = document.createElement("li");
      empty.className = "agent-studio-empty";
      empty.textContent = "Nothing yet. It will ask.";
      list.append(empty);
    }

    actions.replaceChildren();
    const lateActs = new Set(["Who am I", "Go through the door"]);
    const codeAvailable = (inStory && lateActs.has(r.label)) || !!out;
    if ((inStory || inVoid) && !out) actions.append(button("Skip intro", skipIntro, "quiet"));
    if (session && !switching()) actions.append(button(inStory ? "Restart intro" : "Play the intro", restartIntro, "quiet"));
    if (graduated) {
      actions.append(button("Take it to the apartment", graduate, "primary"));
      actions.append(button("Copy the story", () => void copyStory(), "quiet"));
    }
    if (codeAvailable) actions.append(button(showCode ? "Hide the code" : "See the code", () => { showCode = !showCode; render(true); }, "quiet"));

    sheet.hidden = !(showCode && codeAvailable);
    if (!sheet.hidden) {
      const { id, source } = agentSource();
      sheetTitle.textContent = `${name || "MARS"}: the agent file`;
      sheetPath.textContent = `workspace/custom_agents/${id}.py`;
      sheetBody.innerHTML = highlightPython(source);
    }
    if (!foot.textContent.startsWith("Copied")) foot.textContent = "";

    const { chips, exclusive } = offers();
    panel.setOffers(chips, exclusive);
  }

  const unsubAgent = agentState.subscribe(() => {
    greetAfterSkip();
    render();
  });
  const unsubChallenge = session?.onChallenge?.((/** @type {any} */ block) => {
    challenge = block;
    autoStart();
    autoArm();
    giftWave();
    giftMemory();
    render();
  });
  const unsubEnvironment = session?.onEnvironment?.((/** @type {any} */ roster) => {
    environment = roster;
    autoStart();
    giftMemory();
    greetAfterSkip();
    render();
  });
  // The opener arrives as a chat line, not a world frame: re-check the chip gate on a short pulse.
  const spokenPoll = setInterval(() => render(), 1000);
  render();

  return {
    destroy() {
      unsubAgent();
      unsubChallenge?.();
      unsubEnvironment?.();
      clearInterval(spokenPoll);
      if (graduationPoll) clearInterval(graduationPoll);
      panel.setOffers([]);
      panel.setDisplayName(null);
      document.body.classList.remove("story-active");
      document.body.classList.remove("story-agent");
      el.remove();
      sheet.remove();
      whiteout.remove();
    },
  };
}
