// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Agent detail: who the selected agent is, what it can do, and — while the
// Nowhere story runs — the one place that mirrors what the world is asking for.
//
// The story owns the UI only while the world says it is running (`storyRunning`).
// Selecting the story's agent by hand is not the story: the person keeps the rail,
// the scene setup and the challenges, and nothing hides behind a mode they cannot leave.

const STORY_AGENT = "void_agent";
const GRADUATION_AGENT = "demo_agent";
const SKIP_KEY = "innate.nowhere.skip.v1";
const ARMED_KEY = "innate.nowhere.armed";
// Set by the rail's "Play the intro" for an Agent page that is still mounting.
export const PLAY_INTRO_KEY = "innate.nowhere.play";
const INTERNAL_SKILLS = new Set(["innate-os/suggest_user_prompts"]);
const UNMENTIONED_SKILLS = new Set(["innate-os/open_gripper"]); // never part of the story's arc
const WAVE = "innate-os/wave";
const MEMORY = "innate-os/search_memory";
const GRADUATION_MAX_WAIT_MS = 25_000;

// A grant is a turn for the brain, not only a toolset change: the chip says it out loud.
const GRANT_LINES = /** @type {Record<string, string>} */ ({
  "innate-os/head_emotion": "Granted: the HeadEmotion skill. Use it.",
  "innate-os/turn_in_place": "Granted: the TurnInPlace skill. Have a look around.",
  "innate-os/pick_any_object": "Granted: the PickAnyObject skill. Pick it up.",
  "innate-os/navigate_to_position": "Granted: the NavigateToPosition skill. Go to the door.",
  "innate-os/search_memory": "Granted: the SearchMemory skill. Think back.",
});
// What the world does at the top of an act, said to the visitor (not to the brain:
// the act's own brief already tells the robot what changed).
const ACT_OPENERS = /** @type {Record<string, string>} */ ({
  "Pick up the cube": "Something just landed on the floor in front of you.",
  "Go through the door": "A door. Standing on its own, right there.",
});

/** @param {string} id */
const leaf = (id) => id.split("/").at(-1) ?? id;
// Skills are shown by their class names on purpose: the vocabulary the visitor takes away.
/** @param {string} id */
const skillLabel = (id) =>
  leaf(id)
    .split(/[-_]+/)
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join("");

/** @param {Storage} store @param {string} key */
function read(store, key) {
  try {
    return store.getItem(key);
  } catch {
    return null; // private mode: the story still runs, it just forgets between visits
  }
}

/** @param {Storage} store @param {string} key @param {string} value */
function write(store, key, value) {
  try {
    store.setItem(key, value);
  } catch {
    /* see read() */
  }
}

/**
 * @param {HTMLElement} root the agent cockpit
 * @param {ReturnType<import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {any} session sim session (onChallenge/onEnvironment/startChallenge), or null on hardware
 * @param {any} panel the chat panel: offers, narration, onboarding
 * @param {{
 *   transcript: () => string[], spokenCount: () => number, cancelSkill: () => Promise<unknown>,
 *   motion: { seen: () => boolean, lastAt: () => number, reset: () => void },
 *   recalledAt: () => number, turnedAt: () => number, armedAgent?: () => string,
 *   directivesEl?: HTMLElement, showView?: (id: string) => void,
 *   overlay: (cb: (event: any) => void) => () => void,
 * }} opts
 */
export function createAgentStudio(root, agentState, session, panel, opts) {
  // ---- DOM ----------------------------------------------------------------
  const dock = document.createElement("div");
  dock.className = "agent-studio-dock open";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "agent-studio-dock-toggle";
  // The rail's own Agent mark: a four-point sparkle for the autonomous brain.
  toggle.innerHTML =
    '<svg class="agent-studio-dock-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/></svg>' +
    '<span class="microlabel">Agent</span><strong class="agent-studio-title"></strong>' +
    '<span class="agent-studio-dock-chevron"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6,9 12,15 18,9"/></svg></span>';
  const title = /** @type {HTMLElement} */ (toggle.querySelector(".agent-studio-title"));

  const panelEl = document.createElement("section");
  panelEl.className = "agent-studio";
  panelEl.id = "agent-studio-panel";
  panelEl.setAttribute("aria-label", "Agent detail");
  toggle.setAttribute("aria-controls", panelEl.id);

  const persona = document.createElement("span");
  persona.className = "agent-studio-persona";
  const note = document.createElement("p");
  note.className = "agent-studio-note";

  // The personality half of an agent, in the visitor's words.
  const promptRow = document.createElement("form");
  promptRow.className = "agent-studio-prompt";
  promptRow.innerHTML =
    '<span class="microlabel">Prompt</span>' +
    '<textarea rows="2" maxlength="240" aria-label="Robot personality prompt" placeholder="Who is this robot? e.g. a butler who has seen better days"></textarea>' +
    '<button type="submit">Make it so</button>';
  const promptInput = /** @type {HTMLTextAreaElement} */ (promptRow.querySelector("textarea"));

  // What the running agent was actually handed, for an agent nobody is writing here.
  const promptView = document.createElement("details");
  promptView.className = "agent-studio-promptview";
  promptView.innerHTML = "<summary>Prompt</summary><p></p>";
  const promptViewBody = /** @type {HTMLElement} */ (promptView.querySelector("p"));

  const nameRow = document.createElement("form");
  nameRow.className = "agent-studio-name";
  nameRow.innerHTML =
    '<input type="text" maxlength="40" aria-label="Robot name" placeholder="Give it a name"><button type="submit">Name it</button>';
  const nameInput = /** @type {HTMLInputElement} */ (nameRow.querySelector("input"));

  const skills = document.createElement("ul");
  skills.className = "agent-studio-skills";
  const actions = document.createElement("div");
  actions.className = "agent-studio-actions";

  // Change the agent, start and stop it: what the collapsed name cannot do.
  if (opts.directivesEl) panelEl.append(opts.directivesEl);
  panelEl.append(persona, note, promptRow, promptView, nameRow, skills, actions);
  dock.append(toggle, panelEl);
  root.append(dock);

  // Bottom left, where the scene setup sits once the story is over.
  const leaveBtn = document.createElement("button");
  leaveBtn.type = "button";
  leaveBtn.className = "agent-skip-ahead";
  leaveBtn.hidden = true;
  root.append(leaveBtn);

  // The door: the room goes white while the next world compiles behind it.
  const whiteout = document.createElement("div");
  whiteout.className = "agent-whiteout";
  whiteout.setAttribute("aria-hidden", "true");
  root.append(whiteout);

  // ---- state --------------------------------------------------------------
  /** @type {any} */ let challenge = null;
  /** @type {any} */ let environment = null;
  let dockOpen = true;
  let autoStarted = false;
  let armedAttempt = read(sessionStorage, ARMED_KEY) ?? "";
  let waveAttempt = "";
  let arrivalAttempt = "";
  let graduatedAttempt = "";
  let doorAttempt = "";
  let graduationReady = false;
  let staying = false; // "Keep chatting": the ending's offers step aside
  /** @type {ReturnType<typeof setInterval> | null} */ let graduationPoll = null;
  let seenAct = -1;
  let actSpoke = 0; // robot lines when the current act began; chips wait for one more
  let faceSince = 0; // from the persona act on, the camera faces the robot
  let recallShown = 0;
  let turnShown = 0;
  let dragHinted = false;
  let whiteUntil = 0;
  let lastChoice = { text: "", at: 0 };
  let skipPending = false; // skipped ahead; waiting for the Backrooms to land
  let playIntroPending = read(sessionStorage, PLAY_INTRO_KEY) === "1";
  if (playIntroPending) write(sessionStorage, PLAY_INTRO_KEY, "");

  // ---- what the world says -------------------------------------------------
  const active = () => challenge?.active ?? null;
  const story = () => (active()?.runtime?.story ? active() : null);
  const runtime = () => story()?.runtime ?? null;
  const out = () => (active()?.id === "way_out" ? active() : null);
  /** The story owns the interface exactly while the world is running it. */
  const storyRunning = () => !!story() || !!out();
  const switching = () => environment?.switch?.state === "loading";
  const envId = () => environment?.environment?.id ?? "";
  const skipped = () => read(localStorage, SKIP_KEY) === "1";
  const spoken = () => opts.spokenCount();
  /** Persona and name: from the running story, else what the world carried over. */
  const profile = () => {
    const r = runtime();
    const carried = challenge?.profile ?? {};
    return { persona: r?.persona || carried.persona || "", name: r?.name || carried.name || "" };
  };
  const storyAgent = () => agentState.get().agents.find((a) => a.id === STORY_AGENT) ?? null;
  /** The agent the chat panel shows: currentDirective is empty until Start, and a picked agent is still picked. */
  const currentAgent = () => {
    const s = agentState.get();
    const id = s.currentDirective || opts.armedAgent?.() || "";
    return s.agents.find((a) => a.id === id) ?? null;
  };
  const earnedSkills = () => (storyAgent()?.skills ?? []).filter((id) => !UNMENTIONED_SKILLS.has(id));
  /** Whether a robot line names a skill, however it punctuates it. @param {string | undefined} text @param {string} skill */
  const mentions = (text, skill) =>
    (text ?? "").toLowerCase().replace(/[^a-z]/g, "").includes(skillLabel(skill).toLowerCase());

  // ---- running the story ---------------------------------------------------
  // A visit that lands in Nowhere with nothing running is a first run: begin it.
  function autoStart() {
    if (autoStarted || !session || skipped() || envId() !== "void" || !challenge || challenge.active) return;
    if (!challenge.list?.some((/** @type {any} */ c) => c.id === "nowhere")) return;
    autoStarted = true;
    session.startChallenge("nowhere");
  }

  // The story's agent arms itself; nobody should have to find a switch. A just-started
  // attempt arms fresh (resetting chat, memory and skills); one already under way is
  // resumed as it stands, so a reload changes nothing.
  function autoArm() {
    const s = story();
    if (!s || armedAttempt === s.attempt_id || !storyAgent()) return;
    armedAttempt = s.attempt_id;
    write(sessionStorage, ARMED_KEY, armedAttempt);
    const elapsedMs = Math.max(0, Number(s.elapsed_s) || 0) * 1000;
    const fresh = elapsedMs < 8000 || agentState.get().currentDirective !== STORY_AGENT;
    if (fresh) void agentState.setDirective(STORY_AGENT);
    panel.beginOnboarding(fresh, Date.now() - elapsedMs);
    seenAct = -1;
    actSpoke = spoken();
    faceSince = 0;
    turnShown = 0;
    dragHinted = false;
    hideDragHint();
    opts.motion.reset();
    setCamera({ mode: "free", side: 0, back: null });
  }

  // Wave arrives as a gift the moment the persona lands, so the robot can greet in character.
  function giftWave() {
    const s = story();
    if (!s || !profile().persona || waveAttempt === s.attempt_id) return;
    waveAttempt = s.attempt_id;
    if (!agentState.get().activeSkills.has(WAVE)) grant(WAVE, false);
  }

  // The Backrooms feel familiar: the world says so, the robot asks for SearchMemory,
  // and the person grants it like every other skill.
  function arriveInBackrooms() {
    const o = out();
    if (!o || switching() || envId() !== "backrooms" || arrivalAttempt === o.attempt_id) return;
    arrivalAttempt = o.attempt_id;
    actSpoke = spoken();
    panel.clearSuggestedPrompts();
    setTimeout(() => void panel.narrate("Somewhere else. Yellow, this time.", { local: true }), 2500);
  }

  function restartIntro() {
    write(localStorage, SKIP_KEY, "");
    write(sessionStorage, ARMED_KEY, "");
    armedAttempt = "";
    autoStarted = false;
    graduationReady = false;
    staying = false;
    panel.setOffers([]);
    session?.abortChallenge?.();
    if (envId() !== "void") session?.switchEnvironment?.("void");
    render(true);
  }

  // From the white room, skip ahead to where the story was going. From anywhere else in
  // it, just leave: aborting the challenge is what ends story mode, so the rail, the
  // scene setup and the challenges come straight back.
  function leaveStory() {
    write(localStorage, SKIP_KEY, "1");
    panel.setOffers([]);
    agentState.setActiveSkills(earnedSkills());
    // A challenge only starts in the world it is authored for, so the Backrooms have to
    // arrive first; startSkipped() picks it up when they do.
    skipPending = !!story();
    session?.abortChallenge?.();
    if (skipPending) session?.switchEnvironment?.("backrooms");
    render(true);
  }

  function startSkipped() {
    if (!skipPending || switching() || envId() !== "backrooms" || active()) return;
    skipPending = false;
    session?.startChallenge?.("way_out");
  }

  /** @param {string} environmentId where the story's agent goes next, with the skills it earned */
  function graduate(environmentId) {
    agentState.setActiveSkills(earnedSkills());
    session?.switchEnvironment?.(environmentId);
  }

  // The diploma waits for the ending: no skill running and the robot's line delivered.
  /** @param {any} o */
  function armGraduation(o) {
    graduatedAttempt = o.attempt_id;
    graduationReady = false;
    staying = false;
    // Out means out: whatever the robot was doing stops, and it hears it before it speaks again.
    panel.clearSuggestedPrompts();
    void opts.cancelSkill().catch(() => {});
    setTimeout(() => void panel.narrate("You're out. You made it.", { local: true }), 600);
    const before = spoken();
    const deadline = Date.now() + GRADUATION_MAX_WAIT_MS;
    if (graduationPoll) clearInterval(graduationPoll);
    graduationPoll = setInterval(() => {
      if (!((spoken() > before && !panel.isBusy()) || Date.now() > deadline)) return;
      if (graduationPoll) clearInterval(graduationPoll);
      graduationPoll = null;
      graduationReady = true;
      agentState.setActiveSkills(earnedSkills());
      const { name } = profile();
      panel.addNotice(`You built this agent${name ? `: ${name}` : ""}. Its skills are all yours now.`);
      void panel.narrate(
        "The rest of the interface is yours too: scene setup and the challenges sit at the bottom of the stage, and the left rail has Teleop, the map and the settings.",
        { local: true },
      );
      render(true);
    }, 500);
  }

  /** @param {string} skill @param {boolean} [announce] */
  function grant(skill, announce = true) {
    const next = new Set(agentState.get().activeSkills);
    next.add(skill);
    agentState.setActiveSkills([...next]);
    panel.clearSuggestedPrompts();
    if (announce) void panel.submitText(GRANT_LINES[skill] ?? `Granted: the ${skillLabel(skill)} skill.`);
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

  // ---- what the chat offers ------------------------------------------------
  /** Why the chip row shows what it shows; readable in DevTools as data-chips on the panel. */
  let chipReason = "";
  /** The robot asks first: a grant offered before the request spoils the turn-taking.
   *  It may also ask in the turn that ends the previous act, hence the second test.
   *  @param {string[]} wants */
  const asked = (wants) => spoken() > actSpoke || wants.some((skill) => mentions(opts.transcript().at(-1), skill));

  function offers() {
    const r = runtime();
    const o = out();
    if (switching()) {
      chipReason = "frozen";
      return { chips: [], exclusive: true }; // nothing lands while the world changes
    }
    if (o?.state === "passed") {
      if (!graduationReady || staying) {
        chipReason = graduationReady ? "chatting" : "graduating";
        return { chips: [], exclusive: false };
      }
      chipReason = "graduated";
      return {
        chips: [
          { text: "Keep chatting", kind: "persona", onSelect: () => { staying = true; render(true); } },
          { text: "Go to the apartment", kind: "grant", onSelect: () => graduate("apartment") },
          { text: "Go to the crossroads", kind: "grant", onSelect: () => graduate("intersection") },
        ],
        exclusive: true,
      };
    }
    if (o) {
      // Out of the void, one skill left to earn.
      if (agentState.get().activeSkills.has(MEMORY)) {
        chipReason = "memory-granted";
        return { chips: [], exclusive: false };
      }
      const ready = asked([MEMORY]);
      chipReason = ready ? "grants:memory" : "waiting-for-line";
      const chip = { text: `Grant the ${skillLabel(MEMORY)} skill`, kind: "grant", onSelect: () => grant(MEMORY) };
      return { chips: ready ? [chip] : [], exclusive: false };
    }
    if (!r) {
      chipReason = "no-story";
      return { chips: [], exclusive: false };
    }
    const personas = r.personas ?? [];
    if (personas.length) {
      // The persona choice owns the row: nothing else competes with it.
      chipReason = `personas:${personas.length}`;
      const pick = (/** @type {string} */ p) => ({ text: p, kind: "persona", onSelect: () => choose({ persona: p }) });
      return {
        chips: [
          ...personas.map(pick),
          { text: "Surprise me", kind: "persona", onSelect: () => choose({ persona: personas[Math.floor(Math.random() * personas.length)] }) },
        ],
        exclusive: true,
      };
    }
    const wants = (r.wants ?? []).filter(
      (/** @type {string} */ skill) => !agentState.get().activeSkills.has(skill) && !INTERNAL_SKILLS.has(skill) && skill !== WAVE,
    );
    if (!wants.length || !asked(wants)) {
      chipReason = wants.length ? "waiting-for-line" : "granted";
      return { chips: [], exclusive: false };
    }
    chipReason = `grants:${wants.join(",")}`;
    return {
      chips: wants.map((/** @type {string} */ skill) => ({
        text: `Grant the ${skillLabel(skill)} skill`,
        kind: "grant",
        onSelect: () => grant(skill),
      })),
      exclusive: false,
    };
  }

  // ---- the camera ----------------------------------------------------------
  let camera = { mode: /** @type {"free" | "chase"} */ ("free"), side: 0, back: /** @type {number | null} */ (null), height: /** @type {number | null} */ (null) };
  /** @param {{mode: "free" | "chase", side: number, back: number | null, height?: number | null}} want back < 0 faces the robot from the front */
  function setCamera(want) {
    camera = { ...want, height: want.height ?? null };
    document.dispatchEvent(
      new CustomEvent("innate:camera-mode", {
        detail: {
          mode: want.mode,
          side: want.side,
          ...(want.back == null ? {} : { back: want.back }),
          ...(camera.height == null ? {} : { height: camera.height }),
        },
      }),
    );
  }
  /** The story opens on the page's own framing, the robot seen from the front. @param {string} label */
  function cameraFor(label) {
    // Close, high and a little to the right: the robot's back fills the frame, the cube sits ahead of it.
    if (label === "Pick up the cube") return { mode: /** @type {const} */ ("chase"), side: 0.35, back: 0.8, height: 1.3 };
    // The door stands 3 m out: look over the robot's back at it.
    if (label === "Go through the door") return { mode: /** @type {const} */ ("chase"), side: 0, back: 1.4, height: 1.2 };
    // From the persona act on, face the robot for the conversation; fall in behind it once it drives.
    if (faceSince && opts.motion.lastAt() < faceSince) return { mode: /** @type {const} */ ("chase"), side: 0, back: -1.8 };
    if (!opts.motion.seen()) return { mode: /** @type {const} */ ("free"), side: 0, back: null };
    return { mode: /** @type {const} */ ("chase"), side: 0, back: null };
  }

  // The pickup is one uninterruptible action, so MARS cannot narrate it; the interface
  // points at the camera that shows each phase and says so.
  /** @type {HTMLElement | null} */ let cuedTile = null;
  function uncue() {
    cuedTile?.classList.remove("cam-tile-pulse");
    cuedTile?.querySelector(".cam-tile-cue")?.remove();
    cuedTile = null;
  }
  /** @param {string} view @param {string} line */
  function cueCamera(view, line) {
    uncue();
    const tile = [...root.querySelectorAll(".cam-tile")].find(
      (t) => t.querySelector(".cam-tile-label")?.textContent?.trim().toLowerCase() === view,
    );
    if (!(tile instanceof HTMLElement)) return; // already the big view, or not offered
    cuedTile = tile;
    tile.classList.add("cam-tile-pulse");
    const badge = document.createElement("span");
    badge.className = "cam-tile-cue";
    badge.textContent = "click here";
    tile.append(badge);
    void panel.narrate(line, { local: true });
  }

  // The first turn is also the visitor's first chance to learn the 3D view moves.
  /** @type {HTMLElement | null} */ let dragHint = null;
  function hideDragHint() {
    dragHint?.remove();
    dragHint = null;
  }
  function showDragHint() {
    if (dragHinted) return;
    dragHinted = true;
    dragHint = document.createElement("div");
    dragHint.className = "agent-drag-hint";
    dragHint.textContent = "drag to look around";
    root.append(dragHint);
    root.querySelector("canvas")?.addEventListener("pointerdown", hideDragHint, { once: true });
    setTimeout(hideDragHint, 15_000);
    void panel.narrate("MARS is turning to look around. Drag the view with your mouse to look around too.", {
      local: true,
    });
  }

  // ---- render --------------------------------------------------------------
  /** @param {string} text @param {() => void} onClick */
  function actionButton(text, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "agent-studio-action quiet";
    b.textContent = text;
    b.addEventListener("click", onClick);
    return b;
  }

  let renderedKey = "";
  /** @param {boolean} [force] */
  function render(force = false) {
    const s = agentState.get();
    const agent = currentAgent();
    const r = runtime();
    const o = out();
    const { persona: who, name } = profile();
    const env = envId();
    // World frames arrive at physics rate; touch the DOM only when something shown here changed.
    const key = JSON.stringify([
      s.currentDirective, [...s.activeSkills].sort(), agent?.id, agent?.skills, r && { ...r, brief: undefined },
      o && { id: o.id, state: o.state, attempt_id: o.attempt_id }, who, name, env, switching(), dockOpen,
      graduationReady, staying, spoken(), Date.now() < whiteUntil, opts.motion.seen(),
      opts.motion.lastAt() < faceSince, opts.recalledAt(), opts.turnedAt(),
    ]);
    if (!force && key === renderedKey) return;
    renderedKey = key;

    // Act bookkeeping: the chip gate, the grasp camera, the world's opening line.
    const actChanged = !!r && r.act !== seenAct;
    if (actChanged) {
      if (seenAct >= 0) panel.clearSuggestedPrompts();
      if (r.label === "Who am I") faceSince = Date.now();
      // A resumed page must not wait for a line the robot said before the reload.
      actSpoke = seenAct >= 0 || (Number(story()?.elapsed_s) || 0) < 8 ? spoken() : -1;
      const opener = ACT_OPENERS[r.label];
      if (opener) setTimeout(() => void panel.narrate(opener, { local: true }), 300);
      seenAct = r.act;
    }
    if (r) {
      // A new act always re-asserts its camera: the visitor may have dragged or switched away.
      const want = cameraFor(r.label);
      const same = want.mode === camera.mode && want.side === camera.side && want.back === camera.back && (want.height ?? null) === camera.height;
      if (actChanged || !same) setCamera(want);
    }

    // The door: the running skill stops, the world speaks, and it goes white until the Backrooms are in.
    const doorPassed = (r?.finished || (switching() && env === "void")) && armedAttempt && doorAttempt !== armedAttempt;
    if (doorPassed) {
      doorAttempt = armedAttempt;
      panel.clearSuggestedPrompts();
      void opts.cancelSkill().catch(() => {});
      setTimeout(() => void panel.narrate("Through the door.", { local: true }), 400);
    }
    if (r?.finished || (switching() && env === "void")) whiteUntil = Date.now() + 6000;
    if (o && env === "backrooms" && !switching()) whiteUntil = Math.min(whiteUntil, Date.now() + 800);
    const white = Date.now() < whiteUntil;
    whiteout.classList.toggle("on", white);
    if (white) setTimeout(() => render(), whiteUntil - Date.now() + 50);

    if (playIntroPending && environment) {
      playIntroPending = false;
      restartIntro();
    }
    // TurnInPlace draws no overlay, so the first turn arrives as a skill status.
    if (r && opts.turnedAt() > turnShown) {
      turnShown = opts.turnedAt();
      showDragHint();
    }
    // The memory beat has to be seen, not only heard: point the visitor at the map.
    if (o && env === "backrooms" && opts.recalledAt() > recallShown) {
      recallShown = opts.recalledAt();
      setTimeout(() => {
        cueCamera("map", "Look at the map: that is the spot it remembered.");
        setTimeout(uncue, 12_000);
      }, 1500);
    }
    if (o?.state === "passed" && graduatedAttempt !== o.attempt_id) armGraduation(o);

    // Story mode: the world is running the story, and only then.
    const inStory = storyRunning();
    const graduated = !!o && o.state === "passed" && graduationReady;
    document.body.classList.toggle("story-active", inStory);
    document.body.classList.toggle("story-agent", inStory);
    root.classList.toggle("story-active", inStory);
    panelEl.classList.toggle("story", inStory);

    title.textContent = inStory ? name || "MARS" : (agent?.name ?? "No agent");
    panel.setDisplayName(inStory ? name || "MARS" : null);
    persona.textContent = who;
    persona.hidden = !who || !inStory;
    note.textContent = r
      ? r.finished
        ? "Through the door."
        : `Act ${r.act + 1} of ${r.acts}: ${r.label}`
      : graduated
        ? "It found the way out. This is the agent you built."
        : o
          ? "Find the way out."
          : agent
            ? "An agent is a personality plus skills. Flip a skill to add or remove it."
            : "Pick an agent in the chat panel to see its skills.";

    nameRow.hidden = !(r && r.label === "Who am I");
    promptRow.hidden = !inStory;
    if (who && promptInput.dataset.shown !== who && document.activeElement !== promptInput) {
      promptInput.value = who;
      promptInput.dataset.shown = who;
    }
    promptView.hidden = inStory || !agent?.prompt;
    if (!promptView.hidden && promptViewBody.textContent !== agent.prompt) promptViewBody.textContent = agent.prompt;

    renderSkills(inStory, r, agent);

    actions.replaceChildren();
    if (session && !switching()) {
      if (inStory) actions.append(actionButton("Restart intro", restartIntro));
      if (graduated) {
        const repo = document.createElement("a");
        repo.className = "agent-studio-action quiet agent-studio-link";
        repo.href = "https://github.com/innate-inc/innate-os";
        repo.target = "_blank";
        repo.rel = "noopener";
        repo.textContent = "Clone MARS on GitHub, teach it more";
        actions.append(repo);
      }
    }
    leaveBtn.hidden = !(inStory && !!session && !switching());
    leaveBtn.textContent = r ? "Skip intro" : "Leave the story";
    leaveBtn.title = r ? "Skip ahead to the Backrooms" : "Back to the rest of the interface";

    const { chips, exclusive } = offers();
    panelEl.dataset.chips = chipReason;
    panel.setOffers(chips, exclusive);
  }

  /** @param {boolean} inStory @param {any} r @param {any} agent */
  function renderSkills(inStory, r, agent) {
    const s = agentState.get();
    // In the story the roster is the story's agent, and only what it has been offered:
    // unasked rows would spoil the arc.
    const roster = (inStory ? storyAgent()?.skills : agent?.skills) ?? [];
    const unlocked = new Set(r?.unlocked ?? []);
    const wanted = new Set(r?.wants ?? (inStory ? [MEMORY] : []));
    skills.replaceChildren();
    for (const id of roster) {
      if (INTERNAL_SKILLS.has(id)) continue;
      if (r && !unlocked.has(id)) continue;
      if (inStory && !r && UNMENTIONED_SKILLS.has(id) && !s.activeSkills.has(id)) continue;
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
      const flip = document.createElement("button");
      flip.type = "button";
      flip.className = "agent-studio-toggle";
      flip.setAttribute("role", "switch");
      flip.setAttribute("aria-checked", String(granted));
      flip.setAttribute("aria-label", `${granted ? "Revoke" : "Grant"} ${skillLabel(id)}`);
      flip.addEventListener("click", () => agentState.toggleSkill(id));
      row.append(label, state, flip);
      skills.append(row);
    }
    if (inStory && !skills.childElementCount) {
      const empty = document.createElement("li");
      empty.className = "agent-studio-empty";
      empty.textContent = "No skills yet. It will ask for them.";
      skills.append(empty);
    }
  }

  // ---- wiring --------------------------------------------------------------
  const setDockOpen = (/** @type {boolean} */ open) => {
    dockOpen = open;
    dock.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close agent detail" : "Open agent detail");
    render(true);
  };
  toggle.addEventListener("click", () => setDockOpen(!dockOpen));
  setDockOpen(true);
  leaveBtn.addEventListener("click", leaveStory);
  promptRow.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = promptInput.value.trim();
    if (text) choose({ persona: text });
  });
  nameRow.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = nameInput.value.trim();
    if (!name) return;
    choose({ name });
    nameInput.value = "";
  });

  const unsubOverlay = opts.overlay((event) => {
    const skill = String(event?.skill ?? "").replace(/[^a-z0-9]/gi, "").toLowerCase();
    if (!story() || !skill.endsWith("pickanyobject")) return;
    if (event.ev === "run" && event.state === "start") {
      cueCamera("main", "Watch the approach on the MAIN camera. You can keep talking to MARS while it works.");
    } else if (event.ev === "stage" && event.name === "align") {
      cueCamera("arm", "Now the ARM camera, for the grasp.");
    } else if (event.ev === "run" && event.state === "end") {
      uncue();
      opts.showView?.("orbit"); // the grasp was watched on a camera; the story continues in the scene
    }
  });
  const unsubAgent = agentState.subscribe(() => render());
  const unsubChallenge = session?.onChallenge?.((/** @type {any} */ block) => {
    challenge = block;
    startSkipped();
    autoStart();
    autoArm();
    giftWave();
    arriveInBackrooms();
    render();
  });
  const unsubEnvironment = session?.onEnvironment?.((/** @type {any} */ roster) => {
    environment = roster;
    startSkipped();
    autoStart();
    arriveInBackrooms();
    render();
  });
  // The robot's lines arrive as chat, not world frames: re-check the chip gate on a pulse.
  const spokenPoll = setInterval(() => render(), 1000);
  // The stage frames the robot in free orbit whenever it (re)attaches, which can land after
  // this page's first request: forget what was asked and ask again.
  const onCameraReset = () => {
    camera = { mode: "free", side: 0, back: null, height: null };
    render(true);
  };
  document.addEventListener("innate:camera-reset", onCameraReset);
  // The rail's "Play the intro": an event for an open page, the flag for one still mounting.
  const onPlayIntro = () => {
    playIntroPending = false;
    write(sessionStorage, PLAY_INTRO_KEY, "");
    restartIntro();
  };
  document.addEventListener("innate:play-intro", onPlayIntro);
  render();

  return {
    destroy() {
      unsubOverlay();
      unsubAgent();
      unsubChallenge?.();
      unsubEnvironment?.();
      clearInterval(spokenPoll);
      if (graduationPoll) clearInterval(graduationPoll);
      document.removeEventListener("innate:camera-reset", onCameraReset);
      document.removeEventListener("innate:play-intro", onPlayIntro);
      uncue();
      hideDragHint();
      panel.setOffers([]);
      panel.setDisplayName(null);
      document.body.classList.remove("story-active", "story-agent");
      dock.remove();
      leaveBtn.remove();
      whiteout.remove();
    },
  };
}
